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

    Returns (disposition line, grant dict or None). A pass writes the
    dispatch record and returns the grant the session will read. No
    credential is minted here: the receiver mints its own when the fix
    comes back through `/autofix-result`, so nothing on this path can fail
    after the thread has been told a fix is being attempted.
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
        due_at=_deadline(autofix.CALLBACK_DEADLINE_SECONDS),
    )
    LOG.info("%s %s dispatch %s", AUTOFIX_DISPATCHED_MARKER, result.short_id, dispatch_id)
    return decision.disposition, {
        "issue_id": row["issue_id"],
        "short_id": result.short_id,
        "dispatch_id": dispatch_id,
        "callback_token": callback_token,
        "cited_files": sorted({e.file for e in result.evidence if e.file}),
    }


def _deadline(seconds: int) -> str:
    """A due_at `seconds` from now, in the store's ISO-8601 Z form."""
    when = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return when.isoformat().replace("+00:00", "Z")


def _post_findings_reply(d: dict) -> None:
    """Render one delivery's card and post it into its thread.

    Split out of `deliver_findings` so its caller can guard exactly one row.
    A `BotError` keeps its own handling here, because a failed post has a
    retry path the caller's generic guard has no way to offer: the card is
    already rendered and redacted, so it can be stored for the sweep.
    """
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
        # by parsing it back. The grant still returns to the session in the
        # response: a chat outage must not cost the fix.
        schedule_reply_retry(row, json.dumps(card), store=alert_store(), error=exc)


def deliver_findings(body, rows: list[dict]) -> dict:
    """Validate one batch's findings, reply under each card, and answer the
    session with any autofix grants it has earned.

    The response body is the only message the receiver can ever send the
    session that POSTed these findings, and a grant is all it carries.
    The session writes the fix and posts the files back, and the receiver
    opens the pull request itself, so no credential travels down this
    channel and nothing here can fail between the gate and the reply.
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

    for d in deliveries:
        # Per row, not around the loop. Every row here has already advanced to
        # `delivered`, which is terminal and out of the due index, so anything
        # escaping this region leaves those threads permanently silent, and a
        # guard around the whole loop would abandon every row after the one
        # that failed rather than only that row. More than the post can raise
        # something that is not a `BotError`: the chat client is built on
        # first use in the container and reads its credential to do so, so a
        # cold container meeting a throttled parameter store fails here rather
        # than at post time; rendering the card is not exception-free by
        # contract; and scheduling the retry is itself a conditional write
        # that re-raises anything other than the condition failing. The
        # grants are waiting in the response below, so losing the loop would
        # cost the session its fix as well as the remaining threads their
        # replies.
        try:
            _post_findings_reply(d)
        except Exception as exc:  # noqa: BLE001 - one row must never cost the batch
            LOG.error(
                "%s findings reply for %s: %s",
                DELIVERY_FAILURE_MARKER, d["result"].short_id, exc,
            )

    grants = [d["grant"] for d in deliveries if d["grant"]]
    if not grants:
        return respond_json(200, {"autofix": None})
    return respond_json(
        200,
        {
            "autofix": {
                "repo": cfg.target_repo,
                "base_branch": cfg.autofix_base_branch,
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
        # Same shape as every other 200 from this route: the session parses
        # this body rather than reading the status code, and a retried POST
        # is exactly when it would meet a body it cannot parse. There are no
        # grants to re-vend, since the batch that earned them already got
        # them. The 401s above stay plain text: an error carries nothing for
        # the session to read.
        LOG.info("findings for an already-delivered batch; ignoring")
        return respond_json(200, {"autofix": None})

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


# What the record and the session are told when the GitHub sequence did not
# produce a pull request. The App client logged the underlying error.
OPENING_FAILURE = "pull request not opened"


def post_completion(record: dict, reply: str) -> None:
    """Post one autofix outcome into its thread. A chat failure is logged
    under the delivery marker and never reaches the caller."""
    try:
        bot_client().reply_in_thread(record["conversation_id"], record["message_id"], reply)
    except BotError as exc:
        LOG.error(
            "%s autofix completion reply for %s: %s",
            DELIVERY_FAILURE_MARKER, record.get("short_id", ""), exc,
        )


def settle_failed(record: dict, reason: str, *, expected: str) -> None:
    """Advance a record from `expected` to `failed` and tell the thread. A
    lost advance means another writer settled it first and already
    replied."""
    if alert_store().advance_autofix(
        record["dispatch_id"], expected, "failed", extra={"failure": reason}
    ):
        post_completion(record, autofix.completion_reply("failed"))


def settled_outcome(dispatch_id: str) -> dict:
    """The outcome already recorded for a dispatch, for a fix_ready that
    lost its claim: a replayed POST, or the sweep expiring the record
    first. Read fresh, because the record the caller holds predates the
    write it just lost."""
    latest = alert_store().get_autofix_dispatch(dispatch_id) or {}
    outcome = {"status": str(latest.get("status") or "failed")}
    if latest.get("pr_url"):
        outcome["pr_url"] = latest["pr_url"]
    if latest.get("failure"):
        outcome["reason"] = latest["failure"]
    return outcome


def open_fix(record: dict, body: dict) -> dict:
    """The fix_ready branch: validate, claim, open, settle, reply.

    Validation runs before the claim, so a malformed payload costs a 400
    and a failed record and no GitHub call. The claim moves the record to
    `opening` under a short deadline: the sweep cannot expire it under a
    receiver mid-sequence, and a receiver that dies mid-sequence still
    gets its row failed loudly rather than stranded.
    """
    cfg = config()
    short_id = record.get("short_id", "")
    try:
        payload = autofix.parse_fix_payload(body, exclude_paths=cfg.autofix_exclude_paths)
    except autofix.InvalidFixPayload as exc:
        LOG.error("%s %s rejected fix payload: %s", AUTOFIX_FAILED_MARKER, short_id, exc)
        settle_failed(record, str(exc), expected="dispatched")
        return respond_json(400, {"status": "failed", "reason": str(exc)})

    if not alert_store().advance_autofix(
        record["dispatch_id"], "dispatched", "opening",
        due_at=_deadline(autofix.OPENING_DEADLINE_SECONDS),
    ):
        LOG.info("autofix fix_ready for %s already settled; ignoring", record["dispatch_id"])
        return respond_json(200, settled_outcome(record["dispatch_id"]))

    url = github_client().open_fix_pr(
        repo=cfg.target_repo,
        base_sha=payload.base_sha,
        base_branch=cfg.autofix_base_branch,
        branch=autofix.fix_branch(short_id, record["dispatch_id"]),
        files=list(payload.files),
        title=payload.title,
        body=payload.body,
    )
    if not url:
        LOG.error("%s %s %s", AUTOFIX_FAILED_MARKER, short_id, OPENING_FAILURE)
        settle_failed(record, OPENING_FAILURE, expected="opening")
        return respond_json(200, {"status": "failed", "reason": OPENING_FAILURE})

    # Stored raw, so a replay reads back the URL GitHub gave. Encoded only
    # for the thread, where markdown-significant characters would render
    # as formatting instead of a link.
    if alert_store().advance_autofix(
        record["dispatch_id"], "opening", "pr_opened", extra={"pr_url": url}
    ):
        pr_url = safe_callback_url(url, field="pr_url")
        post_completion(record, autofix.completion_reply("pr_opened", pr_url=pr_url))
    return respond_json(200, {"status": "pr_opened", "pr_url": url})


def handle_autofix_result(event: dict) -> dict:
    """Accept a session's outcome for one grant and close the Teams thread.

    Authenticated by the per-dispatch capability token; the record's hash is
    the only credential store. Every advance is conditional, so the callback
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

    if status == "fix_ready":
        try:
            return open_fix(record, body)
        except Exception as exc:  # noqa: BLE001 - a claimed record must settle, never 5xx
            LOG.error(
                "%s %s fix_ready crashed: %s",
                AUTOFIX_FAILED_MARKER, record.get("short_id", ""), exc,
            )
            # The crash may have landed before or after the claim, so try
            # both origins; whichever the record is in, it settles. A store
            # that is itself the thing failing leaves the row to the sweep.
            try:
                for expected in ("opening", "dispatched"):
                    if alert_store().advance_autofix(
                        record["dispatch_id"], expected, "failed",
                        extra={"failure": "receiver crashed"},
                    ):
                        post_completion(record, autofix.completion_reply("failed"))
                        break
            except Exception as settle_exc:  # noqa: BLE001 - logged; the sweep is the backstop
                LOG.error(
                    "%s %s could not settle after the crash: %s",
                    AUTOFIX_FAILED_MARKER, record.get("short_id", ""), settle_exc,
                )
            return respond_json(200, {"status": "failed", "reason": "receiver crashed"})

    run_url = safe_callback_url(str(body.get("run_url") or ""), field="run_url")
    if not alert_store().advance_autofix(record["dispatch_id"], "dispatched", status):
        LOG.info("autofix callback for %s already settled; ignoring", record["dispatch_id"])
        return respond(200, "ok")

    if status == "failed":
        LOG.error(
            "%s %s session reported failure %s",
            AUTOFIX_FAILED_MARKER, record.get("short_id", ""), run_url,
        )
    post_completion(record, autofix.completion_reply(status, run_url=run_url))
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
