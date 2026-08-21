"""Parsing of Sentry `event_alert` webhook bodies into a flat alert record."""

from __future__ import annotations

from dataclasses import dataclass


class InvalidAlertPayload(ValueError):
    """The webhook body is not a usable Sentry issue-alert payload."""


@dataclass(frozen=True)
class SentryAlert:
    """The subset of a Sentry issue alert the receiver acts on."""

    issue_id: str
    issue_api_url: str
    web_url: str
    environment: str
    level: str
    title: str
    culprit: str
    release: str | None
    rule_name: str


def tag_value(event: dict, key: str) -> str | None:
    """Return the value of `key` in the event's tag list, or None."""
    for tag in event.get("tags") or []:
        if isinstance(tag, (list, tuple)) and len(tag) == 2 and tag[0] == key:
            return tag[1]
        if isinstance(tag, dict) and tag.get("key") == key:
            return tag.get("value")
    return None


def parse_alert(payload: dict) -> SentryAlert:
    """Build a SentryAlert from an `event_alert` webhook body.

    Raises InvalidAlertPayload when the body is not a triggered issue alert
    or is missing a field the receiver cannot work without.
    """
    # A signed body is still arbitrary JSON: `null`, a list, or a bare scalar
    # would otherwise reach .get() and escape as an AttributeError.
    if not isinstance(payload, dict):
        raise InvalidAlertPayload(f"body is not a JSON object: {type(payload).__name__}")

    action = payload.get("action")
    if action != "triggered":
        raise InvalidAlertPayload(f"unexpected action: {action!r}")

    data = payload.get("data") or {}
    event = data.get("event") or {}

    issue_id = event.get("issue_id")
    if not issue_id:
        raise InvalidAlertPayload("event is missing issue_id")
    web_url = event.get("web_url")
    if not web_url:
        raise InvalidAlertPayload("event is missing web_url")

    environment = event.get("environment") or tag_value(event, "environment")
    if not environment:
        raise InvalidAlertPayload("event has no environment")

    return SentryAlert(
        issue_id=str(issue_id),
        issue_api_url=event.get("issue_url") or "",
        web_url=web_url,
        environment=environment,
        level=(event.get("level") or "error").lower(),
        title=event.get("title") or "(untitled event)",
        culprit=event.get("culprit") or "",
        release=event.get("release"),
        rule_name=data.get("triggered_rule") or "",
    )
