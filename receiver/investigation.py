"""Deciding which alerts are worth an investigation, and enqueueing them."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from receiver.config import ReceiverConfig
from receiver.models import SentryAlert

LOG = logging.getLogger(__name__)

SHA = re.compile(r"[0-9a-f]{40}")
INVESTIGATED_LEVEL = "error"


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
        # Measured 2026-08-13: every prod and staging issue carries a 40-char
        # SHA. One that does not means something changed upstream, so stop
        # rather than investigate at branch HEAD and report it confidently.
        return False, "release-not-a-sha"

    return True, ""


def due_at(cfg: ReceiverConfig) -> str:
    """When the sweep may first pick a freshly enqueued row up."""
    when = datetime.now(timezone.utc) + timedelta(seconds=cfg.debounce_seconds)
    return when.isoformat().replace("+00:00", "Z")


def enqueue_investigation(alert, conversation_id, message_id, *, cfg, ref, store) -> None:
    """Record an eligible alert as pending, or record why it was skipped.

    May raise. The caller guards it: see `receiver.handler.deliver`, where the
    rule that an enqueue failure must never become a Sentry retry lives.
    """
    ok, reason = eligible(alert, cfg)
    if not ok:
        LOG.info("not investigating %s: %s", ref.short_id, reason)
        return

    enqueued = store.put_investigation(alert, ref, conversation_id, message_id, due_at(cfg))
    LOG.info(
        "investigation for %s at %s: %s",
        ref.short_id,
        (alert.release or "")[:7],
        "enqueued" if enqueued else "already recorded",
    )
