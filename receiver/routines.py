"""Claude Code routines API client. Fires a routine; reads nothing.

Write-only by necessity rather than by choice. Measured 2026-08-13: the API
exposes no configuration read. `GET /v1/claude_code/routines/{id}/fire`
returns 405 (the path is routed, POST only), while `/routines/{id}`,
`/routines`, `/triggers`, and `/environments` return a plain-text
`404 page not found` instead of the API's JSON error shape, and `GET
/v1/models` returns a JSON 403 naming scopes the trigger token lacks. A
routine's repository binding, connector set, and cloud environment can only be
observed by firing a session that reports on itself.
"""

from __future__ import annotations

import enum
import json
import logging

import requests

LOG = logging.getLogger(__name__)

API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
ROUTINE_BETA = "experimental-cc-routine-2026-04-01"
TIMEOUT_SECONDS = 15

# The fire body's `text` field is capped. Over the cap the API returns 400 and
# truncates nothing, so an oversized payload is rejected here rather than sent.
TEXT_LIMIT = 65_536

# Substrings that positively identify a paused routine in a fire response.
# Captured live 2026-08-17 against the paused probe routine (96-validation
# B8): HTTP 400, body {"error":{"message":"Routine is paused.","reason":
# "routine_paused","type":"invalid_request_error"},...}. The quoted JSON
# reason code is matched rather than the prose message, which can be
# reworded without notice.
PAUSED_MARKERS: tuple[str, ...] = ('"routine_paused"',)

# Logged with the raw response body on any failure this module could not
# positively identify. Two response shapes have never been observed: a paused
# routine's, and a 429. Capturing the body when one first arrives is how
# PAUSED_MARKERS gets filled in without needing to reproduce the condition.
UNCLASSIFIED_MARKER = "FIRE_UNCLASSIFIED"


class FireOutcome(enum.Enum):
    """What a fire attempt did, from the caller's point of view."""

    FIRED = "fired"
    PAUSED = "paused"
    RATE_LIMITED = "rate_limited"
    RETRYABLE = "retryable"
    REJECTED = "rejected"


def retry_after_seconds(response) -> int:
    """The `Retry-After` header as an int, or 0 when absent or unusable."""
    try:
        return max(int(response.headers.get("Retry-After")), 0)
    except (TypeError, ValueError):
        return 0


def classify(response) -> tuple[FireOutcome, int]:
    """Map a fire response to an outcome and a retry delay in seconds.

    Unrecognised conditions are deliberately loud. A paused routine is the
    operator's own budget kill switch and must stay silent, but only when it
    is positively identified. Guessing in that direction would swallow a real
    exhaustion event, which nobody chose and everybody needs to see.
    """
    if response.ok:
        return FireOutcome.FIRED, 0

    body = (response.text or "")[:500]
    if any(marker in body for marker in PAUSED_MARKERS):
        return FireOutcome.PAUSED, 0

    # Both branches below log the raw body under one marker, so the first real
    # 429 or paused response is captured where it happens rather than needing a
    # reproduction. That capture is what fills PAUSED_MARKERS in.
    if response.status_code == 429:
        LOG.error("%s HTTP 429 %s", UNCLASSIFIED_MARKER, body)
        return FireOutcome.RATE_LIMITED, retry_after_seconds(response)

    if 500 <= response.status_code < 600:
        return FireOutcome.RETRYABLE, 0

    LOG.error("%s HTTP %s %s", UNCLASSIFIED_MARKER, response.status_code, body)
    return FireOutcome.REJECTED, 0


class RoutineClient:
    """Fires one routine. Holds the trigger token for the container's life."""

    def __init__(self, routine_id: str, token: str):
        self.routine_id = routine_id
        self.token = token
        self.session = requests.Session()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": ROUTINE_BETA,
            "Content-Type": "application/json",
        }

    def fire(self, payload: dict) -> tuple[FireOutcome, int]:
        """Start a routine session carrying `payload`.

        The body's only meaningful field is `text`. Unknown fields are ignored
        silently by the API: the spike observed a `{"message": ...}` body
        return 200, start a session, and deliver nothing.
        """
        text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if len(text) > TEXT_LIMIT:
            LOG.error(
                "fire payload is %d chars, over the %d cap; not sending",
                len(text),
                TEXT_LIMIT,
            )
            return FireOutcome.REJECTED, 0

        try:
            response = self.session.post(
                f"{API_BASE}/v1/claude_code/routines/{self.routine_id}/fire",
                headers=self._headers(),
                json={"text": text},
                timeout=TIMEOUT_SECONDS,
            )
        except OSError as exc:
            LOG.warning("fire transport error: %s", exc)
            return FireOutcome.RETRYABLE, 0

        return classify(response)
