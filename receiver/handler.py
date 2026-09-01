"""Lambda entry point: Function URL routing and the Sentry alert pipeline."""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from receiver import autofix
from receiver.bot import BotError, TeamsBotClient
from receiver.cards import card_summary, render_card
from receiver.config import (
    FIRING_MODES,
    ReceiverConfig,
    assert_ready,
    get_secret,
    load_config,
)
from receiver.findings import (
    InvalidFindings,
    parse_findings,
    render_reply_card,
    reply_summary,
)
from receiver.github_app import GitHubAppClient
from receiver.investigation import enqueue_investigation
from receiver.models import InvalidAlertPayload, parse_alert
from receiver.observability import (
    AUTOFIX_DECLINED_MARKER,
    AUTOFIX_DISPATCHED_MARKER,
    AUTOFIX_FAILED_MARKER,
    DELIVERY_FAILURE_MARKER,
    FINDINGS_REJECTED_MARKER,
    PROBE_LOG_LIMIT,
    PROBE_MARKER,
    flush_sentry,
    init_sentry,
)
from receiver.routines import RoutineClient
from receiver.sentry_api import resolve_issue_ref
from receiver.store import AlertStore, BatchState
from receiver.sweep import run_sweep, schedule_reply_retry

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

init_sentry(os.environ.get("SENTRY_DSN", ""), os.environ.get("SENTASITY_ENV", "prod"))


@functools.cache
def config() -> ReceiverConfig:
    """Load and validate the config once per container. Fails closed."""
    cfg = load_config()
    assert_ready(cfg)
    return cfg


def webhook_secret() -> str:
    """The Sentry internal integration's client secret."""
    return get_secret(config().secret_name("sentry-webhook-secret"))


@functools.cache
def bot_client() -> TeamsBotClient:
    """The Teams bot identity, built once per container."""
    cfg = config()
    return TeamsBotClient(
        tenant_id=cfg.tenant_id,
        app_id=cfg.bot_app_id,
        app_password=get_secret(cfg.secret_name("bot-client-secret")),
        service_url=cfg.service_url,
    )


@functools.cache
def alert_store() -> AlertStore:
    """The DynamoDB alert table, bound once per container."""
    return AlertStore(config().table_name)


@functools.cache
def routine_client() -> RoutineClient:
    """The routines fire client, built once per container."""
    cfg = config()
    return RoutineClient(
        cfg.routine_id, get_secret(cfg.secret_name("routine-trigger-token"))
    )


@functools.cache
def github_client() -> GitHubAppClient:
    """The GitHub App client, built once per container."""
    cfg = config()
    return GitHubAppClient(
        cfg.autofix_app_id, get_secret(cfg.secret_name("github-app-private-key"))
    )


def sweep() -> dict:
    """Run one scheduled pass with the container's collaborators.

    The routine client is built only in a firing mode, mirroring
    `assert_ready`, which requires the trigger config only then. Shadow mode
    must stay inert without it: the trigger-token parameter is a rollout
    prerequisite that lands after the first deploy, and an eager fetch here
    made every sweep tick crash (and trip the error alarm) until it did.
    """
    cfg = config()
    routines = routine_client() if cfg.trigger_mode in FIRING_MODES else None
    return run_sweep(cfg=cfg, store=alert_store(), routines=routines, bot=bot_client())


def respond(status: int, body: str = "") -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "text/plain"},
        "body": body,
    }


def respond_json(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


class MalformedBody(ValueError):
    """The request body is not decodable. Answered with a 400, never a crash."""


def raw_body(event: dict) -> str:
    """Return the request body as text, decoding base64 when Lambda encoded it.

    Raises MalformedBody rather than letting a decode error escape: the Function
    URL is unauthenticated at the platform, so anyone can post arbitrary bytes
    and an uncaught exception here would trip the error alarm on demand.
    """
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise MalformedBody(f"undecodable base64 body: {exc}") from exc
    return body


def sign_body(body: str, secret: str) -> str:
    """Sentry's HMAC-SHA256 of the raw body, hex-encoded.

    Split out from `signature_valid` so `scripts/replay_alert.py` signs with
    this exact function rather than its own copy: two implementations of one
    HMAC format drift into a 401 that reads like a wrong secret.
    """
    return hmac.new(secret.encode(), body.encode("utf-8"), hashlib.sha256).hexdigest()


def signature_valid(body: str, provided: str | None, secret: str) -> bool:
    """Constant-time comparison against Sentry's HMAC-SHA256 of the raw body."""
    if not provided or not provided.isascii():
        # compare_digest raises TypeError on non-ASCII, and a real Sentry
        # signature is always hex, so a non-ASCII header is simply invalid.
        return False
    return hmac.compare_digest(sign_body(body, secret), provided)


def deliver(alert) -> None:
    """Render the card, post it through the bot, and record where it landed.

    Raises BotError on a failed post so `handle_sentry` can return 500 and let
    Sentry's retry and auto-disable machinery see a real failure.
    """
    cfg = config()
    ref = resolve_issue_ref(alert, get_secret(cfg.secret_name("sentry-api-token")))
    card = render_card(alert, ref)
    conversation_id, message_id = bot_client().post_card(
        cfg.channels[alert.environment], card, card_summary(alert, ref)
    )
    alert_store().put_alert(alert, ref, conversation_id, message_id)
    LOG.info(
        "posted %s alert for %s to %s (message %s)",
        alert.level,
        ref.short_id,
        alert.environment,
        message_id,
    )

    # Card delivery and investigation enqueueing have opposite failure
    # semantics. A delivery failure must surface as a 500 so Sentry retries;
    # an enqueue failure must not, because the card has already posted and the
    # retry would post a second one. Anything raised here is swallowed.
    try:
        enqueue_investigation(
            alert, conversation_id, message_id, cfg=cfg, ref=ref, store=alert_store()
        )
    except Exception as exc:  # noqa: BLE001 - see the comment above
        LOG.error("enqueue failed for %s: %s", ref.short_id, exc)


def handle_sentry(event: dict) -> dict:
    try:
        body = raw_body(event)
    except MalformedBody as exc:
        LOG.warning("rejected webhook with an undecodable body: %s", exc)
        return respond(400, "unusable body")

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not signature_valid(body, headers.get("sentry-hook-signature"), webhook_secret()):
        LOG.warning("rejected webhook with invalid signature")
        return respond(401, "invalid signature")

    try:
        alert = parse_alert(json.loads(body))
    except (ValueError, InvalidAlertPayload) as exc:
        LOG.error("unusable webhook payload: %s", exc)
        return respond(400, "unusable payload")

    if alert.environment not in config().environments:
        LOG.info("dropping alert for unserved environment %s", alert.environment)
        return respond(204)

    try:
        deliver(alert)
    except BotError as exc:
        # The marker leads, so the metric filter matches it wherever the rest
        # of the line goes. Returning 500 tells Sentry to retry, but it leaves
        # the invocation successful and therefore invisible to the Errors
        # metric; this line is what the delivery-failure alarm watches.
        LOG.error(
            "%s issue %s: %s", DELIVERY_FAILURE_MARKER, alert.issue_id, exc
        )
        return respond(500, "delivery failed")

    return respond(200, "ok")


def handle_probe(event: dict) -> dict:
    """Record what a probe session reported about its own runtime.

    Unauthenticated on purpose: the probe's stored prompt carries no token,
    because it runs before any batch exists to mint one against. That is safe
    because a log line here proves nothing on its own. The probe's pass
    condition is agreement between this log and the session's own transcript,
    so a forged POST with no matching transcript fails the gate rather than
    passing it. The body is truncated before logging so an open endpoint
    cannot be used to flood the log group.
    """
    try:
        body = raw_body(event)
    except MalformedBody as exc:
        LOG.warning("%s undecodable body: %s", PROBE_MARKER, exc)
        return respond(400, "unusable body")

    LOG.info("%s %s", PROBE_MARKER, body[:PROBE_LOG_LIMIT])
    return respond(200, "ok")


def autofix_grant(result, doc, row: dict) -> tuple[str, dict | None]:
    """Run the gate for one delivered result; stage a grant on pass.

    Returns (disposition line, grant dict or None). The grant is only
    staged: the caller mints one GitHub token for the whole batch and
    withdraws every staged grant if the mint fails, so the thread only
    ever reads "attempting" once a credential actually exists.
    """
    cfg = config()
    decision = autofix.evaluate(result, doc, row, cfg=cfg, store=alert_store())
    if not decision.passed:
        if decision.reason != "disabled":
            LOG.info(
                "%s %s reason=%s",
                AUTOFIX_DECLINED_MARKER, result.short_id, decision.reason,
            )
        return decision.disposition, None

    dispatch_id = str(uuid.uuid4())
    callback_token = secrets.token_urlsafe(32)
    alert_store().put_autofix_dispatch(
        {
            "dispatch_id": dispatch_id,
            "issue_id": row["issue_id"],
            "environment": row["environment"],
            "release": row["release"],
            "short_id": result.short_id,
            "conversation_id": row["conversation_id"],
            "message_id": row["message_id"],
            "callback_token_hash": AlertStore.hash_token(callback_token),
        },
        due_at=_autofix_deadline(),
    )
    LOG.info("%s %s dispatch %s", AUTOFIX_DISPATCHED_MARKER, result.short_id, dispatch_id)
    return decision.disposition, {
        "issue_id": row["issue_id"],
        "short_id": result.short_id,
        "dispatch_id": dispatch_id,
        "callback_token": callback_token,
        "cited_files": sorted({e.file for e in result.evidence if e.file}),
    }


MINT_FAILED_DISPOSITION = "Autofix declined: could not mint a GitHub credential."


def _autofix_deadline() -> str:
    """When the sweep may fail a dispatch that never called back."""
    when = datetime.now(timezone.utc) + timedelta(
        seconds=autofix.CALLBACK_DEADLINE_SECONDS
    )
    return when.isoformat().replace("+00:00", "Z")


def deliver_findings(body, rows: list[dict]) -> dict:
    """Validate one batch's findings, reply under each card, and answer the
    session with any autofix grants it has earned.

    The response body is the vending channel: the session that POSTed these
    findings is the one that will write the fix, and this response is the
    only message the receiver can ever send it. Gates run first, then one
    token is minted for the whole batch, then the cards post; that order
    means a mint failure can still rewrite the disposition line before any
    thread reads it.
    """
    by_issue = {r["issue_id"]: r for r in rows}
    try:
        doc = parse_findings(
            body, batch_id=rows[0]["batch_id"], known_issue_ids=set(by_issue)
        )
    except InvalidFindings as exc:
        # Deliberately leaves the rows `awaiting`, so the deadline still
        # answers the thread rather than leaving it silent.
        LOG.error("%s %s", FINDINGS_REJECTED_MARKER, exc)
        return respond(400, "unusable findings")

    cfg = config()
    deliveries: list[dict] = []
    for result in doc.results:
        row = by_issue[result.issue_id]
        # Claim before posting: the deadline sweep targets the same transition
        # and exactly one of us may write into this thread.
        if not alert_store().advance(
            row["issue_id"], row["environment"], row["release"], "fired", "delivered"
        ):
            LOG.info("row for %s already answered; skipping", result.short_id)
            continue
        try:
            disposition, grant = autofix_grant(result, doc, row)
        except Exception as exc:  # noqa: BLE001 - autofix must never cost the reply
            LOG.error("%s %s gate crashed: %s", AUTOFIX_FAILED_MARKER, result.short_id, exc)
            disposition, grant = "", None
        deliveries.append(
            {"result": result, "row": row, "disposition": disposition, "grant": grant}
        )

    minted = None
    if any(d["grant"] for d in deliveries):
        minted = github_client().mint_autofix_token(cfg.target_repo)
        if minted is None:
            # Withdraw every staged grant: the dispatch records exist but no
            # session will ever call back for them, and the disposition must
            # not promise a fix that cannot start.
            for d in deliveries:
                if not d["grant"]:
                    continue
                alert_store().advance_autofix(
                    d["grant"]["dispatch_id"], "dispatched", "failed"
                )
                LOG.error(
                    "%s %s token mint failed; grant withdrawn",
                    AUTOFIX_FAILED_MARKER, d["result"].short_id,
                )
                d["disposition"] = MINT_FAILED_DISPOSITION
                d["grant"] = None

    for d in deliveries:
        result, row = d["result"], d["row"]
        card, redactions = render_reply_card(result, d["disposition"])
        # Logged at render time, not post time: the count is a property of the
        # redaction pass, and a reply the sweep later retries from storage
        # must not lose it or count it twice.
        LOG.info("REDACTIONS_APPLIED %d for %s", redactions, result.short_id)
        try:
            bot_client().reply_card_in_thread(
                row["conversation_id"],
                row["message_id"],
                card,
                reply_summary(result.short_id),
            )
        except BotError as exc:
            # The findings survived validation and redaction; only the post
            # failed. `delivered` is terminal and out of the due index, so
            # stopping here would lose the reply for good. Hand it to the
            # sweep to retry instead, serialized: `pending_reply` is a string
            # column, and the sweep tells a stored card from legacy markdown
            # by parsing it back. The grant still returns to the session
            # below: a chat outage must not cost the fix.
            schedule_reply_retry(row, json.dumps(card), store=alert_store(), error=exc)

    grants = [d["grant"] for d in deliveries if d["grant"]]
    if not (minted and grants):
        return respond_json(200, {"autofix": None})
    return respond_json(
        200,
        {
            "autofix": {
                "repo": cfg.target_repo,
                "base_branch": cfg.autofix_base_branch,
                "github_token": minted.token,
                "github_token_expires_at": minted.expires_at,
                "callback_url": cfg.autofix_callback_url,
                "grants": grants,
            }
        },
    )


def bearer_token(event: dict) -> str:
    """The Authorization header's bearer value, or an empty string."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    value = headers.get("authorization") or ""
    return value[7:] if value.lower().startswith("bearer ") else ""


def handle_findings(event: dict) -> dict:
    """Accept a session's findings and post them into each card's thread."""
    token = bearer_token(event)
    if not token or not token.isascii():
        LOG.warning("rejected findings with no usable bearer token")
        return respond(401, "unauthorized")

    state, rows = alert_store().claim_batch(token)
    if state is BatchState.UNKNOWN:
        LOG.warning("rejected findings with an unknown reply token")
        return respond(401, "unauthorized")
    if state is BatchState.EXPIRED:
        # A genuine session that reported too late. 401 rather than 200: the
        # token is dead, and the deadline sweep has already answered, or is
        # about to answer, that thread.
        LOG.warning("rejected findings for a batch past its deadline")
        return respond(401, "expired")
    if state is BatchState.DELIVERED:
        LOG.info("findings for an already-delivered batch; ignoring")
        return respond(200, "ok")

    try:
        body = raw_body(event)
    except MalformedBody as exc:
        LOG.warning("rejected findings with an undecodable body: %s", exc)
        return respond(400, "unusable body")

    try:
        parsed = json.loads(body) if body else {}
    except ValueError as exc:
        LOG.warning("rejected findings with unparseable JSON: %s", exc)
        return respond(400, "unusable body")

    return deliver_findings(parsed, rows)


# Result callback URLs come straight off the untrusted request body and are
# interpolated into a Teams thread reply, so a value must prove it is a
# plain clickable link before it earns that placement. Escaping markdown
# delimiters (the `escape_markdown`/`escape_prose` convention used elsewhere
# in this repo) is not an option here: it would turn a real link into
# unclickable text. 2048 mirrors the bounding style of REPLY_LIMIT
# (receiver/findings.py) but is sized for a URL, not prose.
CALLBACK_URL_MAX_LENGTH = 2048
CALLBACK_URL_RE = re.compile(r"^https?://[^\s<>()\[\]`]+$")

# `*`, `_`, and `\` all pass CALLBACK_URL_RE (only whitespace, `<>()[]`, and
# backtick are excluded there) but are markdown-significant on this exact
# rendering surface: MARKDOWN_DELIMITERS (receiver/cards.py:29) and
# PROSE_DELIMITERS (receiver/findings.py:103) both call them out as
# characters Teams reads as formatting. Rejecting them outright, the way the
# shape regex does, is not an option: `_` is common in legitimate GitHub
# URLs (repo and branch names), e.g.
# https://github.com/sentasity/my_repo/pull/42, and dropping the whole URL
# over it would cost a reviewer their real PR link. Percent-encoding instead
# is transparent to HTTP (the encoded and literal forms resolve to the same
# URL), so the link keeps working while Teams no longer sees the characters
# it would otherwise render as formatting. Backtick needs no entry here:
# CALLBACK_URL_RE already rejects it outright.
_URL_MARKDOWN_ENCODING = {"\\": "%5C", "*": "%2A", "_": "%5F"}


def safe_callback_url(value: str, *, field: str) -> str:
    """Return `value`, percent-encoding markdown-significant characters, if
    it is a safe absolute http(s) URL, else "".

    An empty return reuses `autofix.completion_reply`'s existing fallback
    text ("(missing PR URL)" / "(link unavailable)"), so a rejected value
    degrades exactly like an absent one instead of needing its own handling.
    """
    if not value:
        return ""
    if len(value) > CALLBACK_URL_MAX_LENGTH or not CALLBACK_URL_RE.match(value):
        LOG.warning("rejected %s: not a safe absolute http(s) URL", field)
        return ""
    for char, encoded in _URL_MARKDOWN_ENCODING.items():
        value = value.replace(char, encoded)
    return value


def handle_autofix_result(event: dict) -> dict:
    """Accept the workflow's outcome and close the Teams thread.

    Authenticated by the per-dispatch capability token; the record's hash is
    the only credential store. The advance is conditional, so the callback
    and the timeout sweep can both target a record and exactly one wins.
    """
    token = bearer_token(event)
    if not token or not token.isascii():
        LOG.warning("rejected autofix callback with no usable bearer token")
        return respond(401, "unauthorized")

    try:
        body = json.loads(raw_body(event) or "{}")
    except (MalformedBody, ValueError):
        return respond(400, "unusable body")
    if not isinstance(body, dict):
        return respond(400, "unusable body")

    record = alert_store().get_autofix_dispatch(str(body.get("dispatch_id") or ""))
    # `token_hash` is the expensive step, and it runs unconditionally, before
    # the `not record` check, so an unknown dispatch_id and a wrong token
    # both pay for it and neither leaks via that cost. The `or` below still
    # short-circuits `compare_digest` itself when there is no record, so the
    # two rejection paths are not identical work, just close enough that the
    # difference is not the meaningful timing signal.
    token_hash = AlertStore.hash_token(token)
    stored_hash = (record or {}).get("callback_token_hash") or ""
    if not record or not hmac.compare_digest(stored_hash, token_hash):
        LOG.warning("rejected autofix callback: unknown dispatch or bad token")
        return respond(401, "unauthorized")

    status = body.get("status")
    if status not in autofix.CALLBACK_STATUSES:
        LOG.warning("rejected autofix callback with status %r", status)
        return respond(400, "unusable status")

    pr_url = safe_callback_url(str(body.get("pr_url") or ""), field="pr_url")
    run_url = safe_callback_url(str(body.get("run_url") or ""), field="run_url")
    if not alert_store().advance_autofix(
        record["dispatch_id"], "dispatched", status, extra={"pr_url": pr_url}
    ):
        LOG.info("autofix callback for %s already settled; ignoring", record["dispatch_id"])
        return respond(200, "ok")

    if status == "failed":
        LOG.error(
            "%s %s workflow reported failure %s",
            AUTOFIX_FAILED_MARKER, record.get("short_id", ""), run_url,
        )

    reply = autofix.completion_reply(status, pr_url=pr_url, run_url=run_url)
    try:
        bot_client().reply_in_thread(record["conversation_id"], record["message_id"], reply)
    except BotError as exc:
        LOG.error(
            "%s autofix completion reply for %s: %s",
            DELIVERY_FAILURE_MARKER, record.get("short_id", ""), exc,
        )
    return respond(200, "ok")


def route(event: dict) -> dict:
    """Map a Function URL request, or a scheduled invocation, to a response."""
    # EventBridge invokes the function directly, so a scheduled event carries
    # no rawPath and would otherwise fall through to the 404 branch.
    if event.get("source") == "aws.events":
        return sweep()

    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", "GET").upper()
    path = event.get("rawPath") or http.get("path") or "/"

    if path == "/health":
        return respond(200, "ok")
    if path == "/bot" and method == "POST":
        return respond(200, "")
    if path == "/sentry" and method == "POST":
        return handle_sentry(event)
    if path == "/autofix-result" and method == "POST":
        return handle_autofix_result(event)
    # Checked before "/findings": exact-equality routing means the more
    # specific path must come first or it can never be reached.
    if path == "/findings/probe" and method == "POST":
        return handle_probe(event)
    if path == "/findings" and method == "POST":
        return handle_findings(event)

    return respond(404, "not found")


def lambda_handler(event: dict, context) -> dict:
    """Route a Function URL request, draining Sentry before the freeze.

    The flush is in a finally because the paths whose events matter most are
    the ones that raise: an unhandled exception is exactly when the receiver's
    own error report must not be lost.
    """
    try:
        return route(event)
    finally:
        flush_sentry()
