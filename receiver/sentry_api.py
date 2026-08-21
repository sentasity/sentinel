"""Sentry REST lookups the receiver needs at card-render time."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

from receiver.models import SentryAlert

LOG = logging.getLogger(__name__)
TIMEOUT_SECONDS = 5
RULE_PREFIX = re.compile(r"^\[([^\]]+)\]")

_CACHE: dict[str, "IssueRef"] = {}


@dataclass(frozen=True)
class IssueRef:
    """Display identity for a Sentry issue."""

    short_id: str
    project: str


def clear_cache() -> None:
    """Drop the per-container lookup cache (tests and cold-start hygiene)."""
    _CACHE.clear()


def project_from_rule_name(rule_name: str) -> str:
    """Return the `[Project]` prefix of a Sentry rule name, or an empty string."""
    match = RULE_PREFIX.match(rule_name or "")
    return match.group(1) if match else ""


def resolve_issue_ref(alert: SentryAlert, token: str) -> IssueRef:
    """Return the issue's short id and project slug.

    One GET against the issue API supplies both. Any failure degrades to the
    numeric issue id and the project prefix carried in the rule name, because
    a missing display label must never stop the card from posting.
    """
    fallback = IssueRef(f"#{alert.issue_id}", project_from_rule_name(alert.rule_name))
    if not alert.issue_api_url or not token:
        return fallback

    cached = _CACHE.get(alert.issue_api_url)
    if cached is not None:
        return cached

    try:
        response = requests.get(
            alert.issue_api_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_SECONDS,
        )
    except OSError as exc:
        LOG.warning("issue lookup failed for %s: %s", alert.issue_id, exc)
        return fallback

    if not response.ok:
        LOG.warning(
            "issue lookup for %s returned HTTP %s", alert.issue_id, response.status_code
        )
        return fallback

    body = response.json()
    ref = IssueRef(
        short_id=body.get("shortId") or fallback.short_id,
        project=(body.get("project") or {}).get("slug") or fallback.project,
    )
    _CACHE[alert.issue_api_url] = ref
    return ref
