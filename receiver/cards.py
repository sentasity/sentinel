"""Adaptive Card rendering. Pure functions: alert in, card JSON out."""

from __future__ import annotations

from receiver.models import SentryAlert
from receiver.sentry_api import IssueRef

SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
VERSION = "1.4"

# level -> (container style, banner text)
SEVERITY_STYLES: dict[str, tuple[str, str]] = {
    "fatal": ("attention", "💥 FATAL"),
    "error": ("attention", "🔴 ERROR"),
    "warning": ("warning", "🟠 WARNING"),
    "info": ("emphasis", "🔵 INFO"),
    "debug": ("emphasis", "🔵 INFO"),
}
DEFAULT_STYLE = SEVERITY_STYLES["info"]
WARNING_FOOTER = "⚠️ Warnings are not auto-investigated."


# Teams renders Adaptive Card text through a markdown subset, so characters
# that are ordinary in an exception message become formatting. `__main__ in
# <module>` arrived as a bold "main", and Python culprits are full of dunders.
# Only the emphasis and code delimiters are escaped: they are the ones that
# silently eat characters, and escaping the whole punctuation set risks
# rendering literal backslashes on clients that ignore the escape.
MARKDOWN_DELIMITERS = ("\\", "*", "_", "`")


def escape_markdown(text: str) -> str:
    """Backslash-escape the delimiters Teams would otherwise read as formatting."""
    for delimiter in MARKDOWN_DELIMITERS:
        text = text.replace(delimiter, f"\\{delimiter}")
    return text


def style_for(level: str) -> tuple[str, str]:
    """Return the (container style, banner text) pair for a Sentry level."""
    return SEVERITY_STYLES.get((level or "").lower(), DEFAULT_STYLE)


def alert_kind(alert: SentryAlert) -> str:
    """Human label for what fired, taken from the rule name's suffix."""
    name = alert.rule_name or ""
    if "]" in name:
        name = name.split("]", 1)[1]
    return name.split(" - ", 1)[0].strip() or "Alert"


def _banner(alert: SentryAlert) -> dict:
    style, text = style_for(alert.level)
    return {
        "type": "Container",
        "style": style,
        "bleed": True,
        "items": [
            {
                "type": "ColumnSet",
                "columns": [
                    {
                        "type": "Column",
                        "width": "auto",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": text,
                                "weight": "Bolder",
                                "size": "Medium",
                                "wrap": False,
                            }
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "horizontalAlignment": "Right",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": f"{alert.environment.upper()} · {alert_kind(alert)}",
                                "weight": "Bolder",
                                "horizontalAlignment": "Right",
                                "wrap": False,
                            }
                        ],
                    },
                ],
            }
        ],
    }


def card_summary(alert: SentryAlert, ref: IssueRef) -> str:
    """Plain-text summary used for the Teams toast notification."""
    _, banner = style_for(alert.level)
    return f"{banner} {ref.short_id}: {alert.title}"


def render_card(alert: SentryAlert, ref: IssueRef) -> dict:
    """Render the full Adaptive Card for one alert."""
    body: list[dict] = [
        _banner(alert),
        {
            "type": "TextBlock",
            "text": escape_markdown(alert.title),
            "weight": "Bolder",
            "wrap": True,
            "maxLines": 3,
            "spacing": "Medium",
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Project", "value": escape_markdown(ref.project or "—")},
                {"title": "Issue", "value": escape_markdown(ref.short_id)},
                {"title": "Culprit", "value": escape_markdown(alert.culprit or "—")},
            ],
        },
    ]

    if (alert.level or "").lower() == "warning":
        body.append(
            {
                "type": "TextBlock",
                "text": WARNING_FOOTER,
                "isSubtle": True,
                "wrap": True,
                "spacing": "Medium",
            }
        )

    return {
        "$schema": SCHEMA,
        "type": "AdaptiveCard",
        "version": VERSION,
        "body": body,
        "actions": [
            {"type": "Action.OpenUrl", "title": "Open in Sentry", "url": alert.web_url}
        ],
    }
