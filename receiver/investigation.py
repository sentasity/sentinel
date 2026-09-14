"""Deciding which alerts are worth an investigation, and enqueueing them."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from receiver.bot import thread_link
from receiver.config import ReceiverConfig
from receiver.models import SentryAlert
from receiver.sweep import NOT_STARTED_REASONS

LOG = logging.getLogger(__name__)

SHA = re.compile(r"[0-9a-f]{40}")
INVESTIGATED_LEVEL = "error"

# How many times a repeat alert may re-run an investigation that failed. One:
# a failure is usually the routine or the chat surface having a bad quarter
# hour, and the next repeat is a fair second try, but an investigation that
# fails twice on the same code has something wrong that a third session will
# not fix, and a hot issue re-alerts daily for as long as it stays hot.
MAX_REQUEUES = 1

POINTER_PREFIX = "🔁 Not re-investigated: "


def eligible(alert: SentryAlert, cfg: ReceiverConfig) -> tuple[bool, str]:
    """Whether `alert` should be investigated, and the skip reason if not.

    Ordered cheapest first and stops at the first failure. The skip cache is
    deliberately absent here: it is a conditional write in the store, so
    checking it separately would be a read the write already performs.
    """
    if alert.level != INVESTIGATED_LEVEL:
        # The card's "not auto-investigated" footer is decided from the same
        # value, so these two must never disagree.
        return False, "level"

    if alert.environment not in cfg.environments:
        return False, "environment"

    if not alert.release:
        return False, "no-release"

    if cfg.release_to_sha_is_identity and not SHA.fullmatch(alert.release):
        # Under identity mapping the release IS the commit, so one that is
        # not a SHA means something changed upstream. Stop rather than
        # investigate at branch HEAD and report on the wrong program.
        return False, "release-not-a-sha"

    return True, ""


def due_at(cfg: ReceiverConfig) -> str:
    """When the sweep may first pick a freshly enqueued row up."""
    when = datetime.now(timezone.utc) + timedelta(seconds=cfg.debounce_seconds)
    return when.isoformat().replace("+00:00", "Z")


def pointer_text(existing: dict, alert: SentryAlert, cfg: ReceiverConfig) -> str:
    """What a repeat alert's thread is told about the investigation it found.

    Worded for the person reading the channel: the card they are looking at
    has no thread of its own, and without this line it reads as skipped.
    """
    release = (alert.release or "")[:7]
    link = thread_link(existing["conversation_id"], existing["message_id"], cfg.tenant_id)
    status = existing.get("status")
    if status in ("pending", "fired"):
        return (
            f"{POINTER_PREFIX}an investigation of this issue on release {release} is "
            f"already under way. Findings will be posted under {link}"
        )
    if status == "failed":
        return (
            f"{POINTER_PREFIX}the investigation of this issue on release {release} "
            f"failed and has already been retried. Earlier thread: {link}"
        )
    text = f"{POINTER_PREFIX}this issue was already investigated on release {release}."
    if existing.get("confidence"):
        outcome = f"confidence {existing['confidence']}"
        if existing.get("fixability"):
            outcome += f", fixability {existing['fixability']}"
        text += f" Findings ({outcome}): {link}"
    else:
        text += f" Findings: {link}"
    return text


def enqueue_investigation(
    alert, conversation_id, message_id, *, cfg, ref, store, bot
) -> None:
    """Record an eligible alert as pending, or say why it was not.

    One investigation per issue per environment per release: the first alert
    enqueues, and a repeat on the same release finds that row. Sentry sends
    repeats on purpose, since a frequency rule re-fires for as long as an
    issue stays hot, and each one posts a card with no thread of its own.
    A repeat therefore answers its own card with a pointer to the thread
    that has the findings, or, when the earlier attempt failed, takes the
    row over and runs the investigation again under the new card.

    May raise. The caller guards it: see `receiver.handler.deliver`, where the
    rule that an enqueue failure must never become a Sentry retry lives.
    """
    ok, reason = eligible(alert, cfg)
    if not ok:
        LOG.info("not investigating %s: %s", ref.short_id, reason)
        return

    release = alert.release or ""
    if store.put_investigation(alert, ref, conversation_id, message_id, due_at(cfg)):
        LOG.info("investigation for %s at %s: enqueued", ref.short_id, release[:7])
        return

    existing = store.get_investigation(alert.issue_id, alert.environment, release)
    if existing is None:
        # The row was there a moment ago and is not now: a replay's `--reset`
        # deleted it between the two calls. Its replay will enqueue.
        LOG.info("investigation for %s at %s: already recorded", ref.short_id, release[:7])
        return

    if existing.get("status") == "failed" and store.requeue_failed(
        alert.issue_id,
        alert.environment,
        release,
        conversation_id,
        message_id,
        due_at(cfg),
        max_requeues=MAX_REQUEUES,
        notice_kinds=NOT_STARTED_REASONS,
    ):
        # The sweep announces the retry in the new thread when it fires.
        LOG.info(
            "investigation for %s at %s: requeued after a failed attempt",
            ref.short_id, release[:7],
        )
        return

    if existing.get("status") == "failed":
        # The requeue lost. Either the retry budget is spent, or another
        # repeat arriving seconds before this one spent it, in which case the
        # row now belongs to that repeat's card and the pointer should say
        # so. Read it again rather than describe the thread that failed.
        existing = store.get_investigation(alert.issue_id, alert.environment, release) or existing

    LOG.info(
        "investigation for %s at %s: already recorded (%s)",
        ref.short_id, release[:7], existing.get("status"),
    )
    try:
        bot.reply_in_thread(conversation_id, message_id, pointer_text(existing, alert, cfg))
    except Exception as exc:  # noqa: BLE001 - the card is up; the pointer is a courtesy
        LOG.warning("could not point %s at its earlier thread: %s", ref.short_id, exc)
