"""The scheduled pass: group pending investigations, fire them, mind deadlines.

Runs on the same Lambda as the Function URL routes, invoked directly by an
EventBridge rule rather than over the URL, so this path carries no HMAC and is
not reachable from the internet.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from receiver.findings import reply_summary
from receiver.observability import (
    AUTOFIX_FAILED_MARKER,
    DELIVERY_FAILURE_MARKER,
    WINDOW_EXHAUSTED_MARKER,
)
from receiver.routines import FireOutcome
from receiver.store import utc_now

LOG = logging.getLogger(__name__)

# A `Retry-After` at or under this is read as the daily routine-run cap and
# earns one bounded retry. Anything longer is read as weekly-window
# exhaustion, which nobody chose and which is therefore logged loudly.
#
# A heuristic, and flagged as one: the spike recorded that both limits return
# `429 rate_limit_error` with `Retry-After`, and the daily cap was never
# observed on this account. Replace with a real discriminator once a 429 is
# seen in practice.
RETRY_TTL_SECONDS = 4 * 60 * 60


def _at(seconds: int) -> str:
    """An ISO-8601 Z timestamp `seconds` from now."""
    when = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return when.isoformat().replace("+00:00", "Z")


def fire_group(rows: list[dict], *, cfg, store, routines, bot) -> str:
    """Fire one batch and tell its threads. Returns the outcome name.

    The fire and the announcement are separate on purpose: the announcement
    is cosmetic and must never decide what happens to a row, so it runs after
    every state transition is already durable.
    """
    outcome = _fire(rows, cfg=cfg, store=store, routines=routines)
    announce(rows, outcome, store=store, bot=bot)
    return outcome


def _fire(rows: list[dict], *, cfg, store, routines) -> str:
    """Attempt one fire. Returns the outcome name for the sweep's summary."""
    day = datetime.now(timezone.utc).date().isoformat()
    if not store.claim_fire(day, cfg.daily_fire_cap):
        LOG.info("daily fire cap of %d reached; %d rows held", cfg.daily_fire_cap, len(rows))
        return "throttled"

    batch_id = str(uuid.uuid4())
    reply_token = secrets.token_urlsafe(32)
    first = rows[0]
    outcome, delay = routines.fire(
        {
            "project": first.get("project", ""),
            "issue_ids": [r["issue_id"] for r in rows],
            "release": first.get("release", ""),
            "batch_id": batch_id,
            "reply_token": reply_token,
        }
    )

    if outcome is FireOutcome.FIRED:
        deadline = _at(cfg.deadline_seconds)
        for r in rows:
            store.advance(
                r["issue_id"], r["environment"], r["release"], "pending", "fired",
                due_at=deadline,
                extra={
                    "batch_id": batch_id,
                    "reply_token_hash": store.hash_token(reply_token),
                },
            )
        return "fired"

    if outcome is FireOutcome.PAUSED:
        # INFO on purpose. A pause is deliberate, so it is counted and not
        # announced; exhaustion below is the version nobody chose.
        LOG.info("routine paused; holding %d rows", len(rows))
        return "paused"

    if outcome is FireOutcome.RATE_LIMITED and delay and delay <= RETRY_TTL_SECONDS:
        for r in rows:
            store.advance(
                r["issue_id"], r["environment"], r["release"], "pending", "pending",
                due_at=_at(delay),
                extra={"attempt": int(r.get("attempt", 0)) + 1},
            )
        LOG.info("daily cap hit; retrying %d rows in %ds", len(rows), delay)
        return "retry"

    if outcome is FireOutcome.RATE_LIMITED:
        LOG.error("%s skipping %d rows", WINDOW_EXHAUSTED_MARKER, len(rows))
        return "exhausted"

    if outcome is FireOutcome.RETRYABLE:
        LOG.warning("transient fire failure; holding %d rows", len(rows))
        return "retryable"

    LOG.error("fire rejected for batch %s; marking %d rows failed", batch_id, len(rows))
    for r in rows:
        store.advance(r["issue_id"], r["environment"], r["release"], "pending", "failed")
    return "rejected"


# Posted into every alert thread the moment its investigation starts, so the
# channel knows an answer is coming before the session takes its 15 minutes.
FIRED_ACK = "🔍 Investigating this alert. Findings will be posted in this thread."

# Why no investigation started, keyed by the outcome `_fire` returned. Worded
# for the person reading the channel rather than for the log: what they need
# from it is whether findings are still coming, so every reason that will be
# retried says so.
NOT_STARTED_REASONS: dict[str, str] = {
    "throttled": "the day's investigation budget is spent",
    "paused": "automated investigation is paused",
    "retry": "the Claude API is rate limiting; another attempt is queued",
    "exhausted": "the Claude subscription's usage window is exhausted",
    "retryable": "the Claude API could not be reached; another attempt is queued",
    "rejected": "the Claude API rejected the request",
}
NOT_STARTED_PREFIX = "🚫 No automated investigation was started: "


def post_notice(row: dict, text: str, *, bot) -> None:
    """Post one status line into a row's alert thread. Never raises.

    Deliberately quieter than `DELIVERY_FAILURE_MARKER`: this line says an
    investigation started or did not, and losing it costs a reader some
    context. The findings reply that matters carries its own retry, and the
    deadline sweep still answers a thread that hears nothing else.
    """
    try:
        bot.reply_in_thread(row["conversation_id"], row["message_id"], text)
    except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
        LOG.warning(
            "could not post status for %s: %s", row.get("short_id", row["issue_id"]), exc
        )


def announce(rows: list[dict], outcome: str, *, store, bot) -> None:
    """Tell every thread in the group what the fire attempt did.

    A fired group is announced unconditionally: its rows have just left
    `pending`, so this runs once by construction. Every other outcome holds
    the rows where the next sweep finds them again, which is why the notice
    is claimed first. Without the claim a paused routine would repost its
    reason into the thread once a minute for as long as it stayed paused.
    """
    if outcome == "fired":
        for r in rows:
            post_notice(r, FIRED_ACK, bot=bot)
        return

    reason = NOT_STARTED_REASONS.get(outcome)
    if not reason:
        return

    text = f"{NOT_STARTED_PREFIX}{reason}."
    for r in rows:
        if store.claim_notice(r["issue_id"], r["environment"], r["release"], outcome):
            post_notice(r, text, bot=bot)


def group_pending(rows: list[dict], max_batch: int) -> list[list[dict]]:
    """Collapse pending rows into fireable batches.

    Grouped by project, release, and environment, so one bad deploy becomes
    one investigation. Split at `max_batch` because a session's output degrades
    with the number of unrelated issues it is asked to hold at once.
    """
    buckets: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        key = (r.get("project", ""), r.get("release", ""), r.get("environment", ""))
        buckets.setdefault(key, []).append(r)

    groups: list[list[dict]] = []
    for bucket in buckets.values():
        for start in range(0, len(bucket), max_batch):
            groups.append(bucket[start : start + max_batch])
    return groups


DEADLINE_REPLY = (
    "An automated investigation was started for this alert and did not report "
    "back in time. No findings are available."
)

# How many times a validated findings reply may be posted before its row is
# abandoned as `failed`. The design bounds delivery retries and fire retries
# by one shared ceiling; this constant is that ceiling.
MAX_ATTEMPTS = 3

# A failed reply re-enters the due index this far out. The real pacing is the
# once-a-minute sweep; this only needs to be soon enough not to add to it.
REPLY_RETRY_SECONDS = 60


def schedule_reply_retry(row: dict, text: str, *, store, error) -> bool:
    """Queue another attempt at a findings reply whose post just failed.

    Called with the row held at `delivered`, the claim that preceded the
    post. `delivered` is terminal and out of the due index, so leaving the
    row there would lose a reply that already passed validation and
    redaction. Returning it to `fired` with a near-term deadline puts it back
    where the sweep will retry the stored text. At MAX_ATTEMPTS failed posts
    the row is marked `failed` and the delivery-failure marker logged: the
    alarm fires on exhaustion, never on a blip the next sweep absorbs.
    """
    failures = int(row.get("delivery_attempt", 0)) + 1
    short_id = row.get("short_id", row["issue_id"])
    if failures >= MAX_ATTEMPTS:
        store.advance(
            row["issue_id"], row["environment"], row["release"], "delivered", "failed"
        )
        LOG.error(
            "%s findings reply for %s failed %d times; giving up: %s",
            DELIVERY_FAILURE_MARKER, short_id, failures, error,
        )
        return False
    store.advance(
        row["issue_id"], row["environment"], row["release"], "delivered", "fired",
        due_at=_at(REPLY_RETRY_SECONDS),
        extra={"delivery_attempt": failures, "pending_reply": text},
    )
    LOG.warning(
        "findings reply for %s failed (attempt %d of %d); retrying next sweep: %s",
        short_id, failures, MAX_ATTEMPTS, error,
    )
    return True


def _stored_card(pending: str) -> dict | None:
    """The stored reply parsed as a card, or None when it is markdown text.

    `pending_reply` holds a serialized Adaptive Card since findings replies
    became cards; rows written before that ship held markdown, and the type
    check keeps them posting exactly as they did.
    """
    try:
        parsed = json.loads(pending)
    except ValueError:
        return None
    if isinstance(parsed, dict) and parsed.get("type") == "AdaptiveCard":
        return parsed
    return None


def _retry_pending_reply(r: dict, *, store, bot) -> None:
    """Repost a stored findings reply whose earlier post failed."""
    # Same claim-before-post discipline as the first delivery attempt.
    if not store.advance(
        r["issue_id"], r["environment"], r["release"], "fired", "delivered"
    ):
        return
    pending = r["pending_reply"]
    try:
        card = _stored_card(pending)
        if card:
            bot.reply_card_in_thread(
                r["conversation_id"],
                r["message_id"],
                card,
                reply_summary(r.get("short_id", r["issue_id"])),
            )
        else:
            bot.reply_in_thread(r["conversation_id"], r["message_id"], pending)
    except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
        schedule_reply_retry(r, pending, store=store, error=exc)
        return
    LOG.info(
        "delivered findings reply for %s on attempt %d",
        r.get("short_id", r["issue_id"]),
        int(r.get("delivery_attempt", 0)) + 1,
    )


def expire_overdue(*, store, bot) -> int:
    """Answer every fired row whose deadline passed. Returns fallbacks posted.

    Two kinds of row share the awaiting state. A row without `pending_reply`
    never reported and gets the deadline fallback. A row carrying one DID
    report (its findings were validated and rendered; only the post failed),
    so "no findings are available" would be a lie; the stored text is retried
    instead, bounded by MAX_ATTEMPTS. The fallback's claim also requires
    `pending_reply` to be absent at write time, so a stale index read can
    never land the fallback on a row the retry path has re-armed.
    """
    expired = 0
    for r in store.query_due("awaiting", utc_now()):
        if r.get("pending_reply"):
            _retry_pending_reply(r, store=store, bot=bot)
            continue
        # Claim the row before posting. The findings handler targets the same
        # transition, and exactly one of us may write into this thread.
        if not store.advance(
            r["issue_id"], r["environment"], r["release"], "fired", "failed",
            require_absent="pending_reply",
        ):
            continue
        try:
            bot.reply_in_thread(r["conversation_id"], r["message_id"], DEADLINE_REPLY)
            expired += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
            LOG.error(
                "%s deadline reply for %s: %s",
                DELIVERY_FAILURE_MARKER,
                r.get("short_id", r["issue_id"]),
                exc,
            )
    return expired


AUTOFIX_TIMEOUT_REPLY = "Autofix failed: the fix run never reported back."


def expire_autofix(*, store, bot) -> int:
    """Fail every dispatch whose callback deadline passed. Returns count told.

    The advance is conditioned on `dispatched`, the same claim the callback
    route makes, so a late callback and this sweep cannot both write into
    the thread.
    """
    expired = 0
    for r in store.query_due("autofix", utc_now()):
        if not store.advance_autofix(
            r["dispatch_id"], "dispatched", "failed",
            extra={"failure": "callback timeout"},
        ):
            continue
        LOG.error(
            "%s %s callback timeout",
            AUTOFIX_FAILED_MARKER, r.get("short_id", r["dispatch_id"]),
        )
        try:
            bot.reply_in_thread(r["conversation_id"], r["message_id"], AUTOFIX_TIMEOUT_REPLY)
            expired += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
            LOG.error(
                "%s autofix timeout reply for %s: %s",
                DELIVERY_FAILURE_MARKER, r.get("short_id", r["dispatch_id"]), exc,
            )
    return expired


def run_sweep(*, cfg, store, routines, bot) -> dict:
    """One scheduled pass. Returns a summary for the invocation log."""
    summary = {
        "expired": expire_overdue(store=store, bot=bot),
        "autofix_expired": expire_autofix(store=store, bot=bot),
    }

    if cfg.trigger_mode not in ("auto",):
        # Shadow runs the real grouping and stops just short of the fire.
        # The mode exists to answer whether batching groups what a human
        # would group, and a bare row count cannot: six issues from one bad
        # deploy must read as one investigation, not six.
        pending = store.query_due("pending", utc_now())
        groups = group_pending(pending, cfg.max_batch_issues)
        shapes = [
            {
                "project": g[0].get("project", ""),
                "environment": g[0].get("environment", ""),
                "release": (g[0].get("release", "") or "")[:12],
                "issue_ids": [r["issue_id"] for r in g],
            }
            for g in groups
        ]
        for shape in shapes:
            LOG.info("shadow: would investigate %s", shape)
        LOG.info("shadow: %d pending rows form %d groups", len(pending), len(shapes))
        summary["shadow"] = {"rows": len(pending), "groups": shapes}
        # The same terminal line the firing branch ends on: it is the one
        # greppable per-tick liveness signal (validation A8 counts it), so
        # shadow mode must emit it too.
        LOG.info("sweep summary: %s", summary)
        return summary

    groups = group_pending(store.query_due("pending", utc_now()), cfg.max_batch_issues)
    for group in groups[: cfg.per_sweep_fire_cap]:
        outcome = fire_group(group, cfg=cfg, store=store, routines=routines, bot=bot)
        summary[outcome] = summary.get(outcome, 0) + 1

    held = len(groups) - min(len(groups), cfg.per_sweep_fire_cap)
    if held:
        # Never silent: a dropped group must not read as "everything fired".
        LOG.info("per-sweep cap of %d reached; %d groups held", cfg.per_sweep_fire_cap, held)
        summary["held"] = held

    LOG.info("sweep summary: %s", summary)
    return summary
