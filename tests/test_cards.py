"""Adaptive Card rendering for Sentry alerts."""

import copy
import dataclasses

import pytest

from receiver.cards import SEVERITY_STYLES, card_summary, render_card
from receiver.models import parse_alert
from receiver.sentry_api import IssueRef
from tests.conftest import load_fixture

REF = IssueRef(short_id="SCANNERS-7X", project="scanners")


def alert_at_level(level):
    payload = copy.deepcopy(load_fixture("sentry-webhook-alert.json"))
    payload["data"]["event"]["level"] = level
    return parse_alert(payload)


def banner_container(card):
    return card["body"][0]


@pytest.mark.parametrize(
    "level,style,banner",
    [
        ("fatal", "attention", "💥 FATAL"),
        ("error", "attention", "🔴 ERROR"),
        ("warning", "warning", "🟠 WARNING"),
        ("info", "emphasis", "🔵 INFO"),
    ],
)
def test_render_card_styles_the_banner_by_severity(level, style, banner):
    card = render_card(alert_at_level(level), REF)
    container = banner_container(card)

    assert container["style"] == style
    assert container["items"][0]["columns"][0]["items"][0]["text"] == banner


def test_unknown_level_falls_back_to_info_styling():
    card = render_card(alert_at_level("trace"), REF)

    assert banner_container(card)["style"] == "emphasis"


def test_card_declares_adaptive_card_1_4():
    card = render_card(alert_at_level("error"), REF)

    assert card["type"] == "AdaptiveCard"
    assert card["version"] == "1.4"
    assert card["$schema"] == "http://adaptivecards.io/schemas/adaptive-card.json"


def test_banner_carries_environment_and_rule_kind():
    card = render_card(alert_at_level("error"), REF)
    right = banner_container(card)["items"][0]["columns"][1]["items"][0]["text"]

    assert "STAGING" in right
    assert "New Issue" in right


def test_severity_styles_cover_every_sentry_level():
    assert set(SEVERITY_STYLES) == {"fatal", "error", "warning", "info", "debug"}


def test_card_summary_leads_with_banner_and_short_id():
    summary = card_summary(alert_at_level("error"), REF)

    assert summary.startswith("🔴 ERROR SCANNERS-7X: ")


def facts(card):
    factset = next(b for b in card["body"] if b["type"] == "FactSet")
    return {f["title"]: f["value"] for f in factset["facts"]}


def title_of(card):
    return card["body"][1]["text"]


def test_card_lists_the_triage_facts():
    """No Environment fact; the banner carries it (see the banner test above)."""
    card = render_card(alert_at_level("error"), REF)

    assert facts(card) == {
        "Project": "scanners",
        "Issue": "SCANNERS-7X",
        "Culprit": r"\_\_main\_\_ in <module>",
    }


def test_dunder_culprits_are_not_eaten_as_markdown():
    """Teams renders card text through markdown, so `__main__` came out bold.

    Python culprits are full of dunders (`__init__`, `__main__`, `__call__`),
    and the golden files cannot catch this: they hold the JSON we send, not what
    Teams draws from it.
    """
    card = render_card(alert_at_level("error"), REF)

    assert facts(card)["Culprit"] == r"\_\_main\_\_ in <module>"


def test_emphasis_characters_in_a_title_are_escaped():
    alert = dataclasses.replace(alert_at_level("error"), title="ValueError: got *args not **kwargs")

    card = render_card(alert, REF)

    assert title_of(card) == r"ValueError: got \*args not \*\*kwargs"


def test_escaping_leaves_ordinary_text_alone():
    alert = dataclasses.replace(alert_at_level("error"), title="Connection reset by peer")

    assert title_of(render_card(alert, REF)) == "Connection reset by peer"


def test_card_shows_the_wrapped_issue_title():
    alert = alert_at_level("error")
    card = render_card(alert, REF)
    title_block = card["body"][1]

    assert title_block["type"] == "TextBlock"
    assert title_block["text"] == alert.title
    assert title_block["wrap"] is True
    assert title_block["maxLines"] == 3


def test_card_links_back_to_sentry():
    alert = alert_at_level("error")
    card = render_card(alert, REF)

    assert card["actions"] == [
        {"type": "Action.OpenUrl", "title": "Open in Sentry", "url": alert.web_url}
    ]


def test_warning_card_carries_the_not_auto_investigated_footer():
    card = render_card(alert_at_level("warning"), REF)
    footer = card["body"][-1]

    assert footer["text"] == "⚠️ Warnings are not auto-investigated."
    assert footer["isSubtle"] is True


def test_error_card_has_no_footer():
    card = render_card(alert_at_level("error"), REF)

    assert all(
        b.get("text") != "⚠️ Warnings are not auto-investigated." for b in card["body"]
    )


def test_missing_culprit_renders_a_dash():
    alert = dataclasses.replace(alert_at_level("error"), culprit="")
    card = render_card(alert, REF)

    assert facts(card)["Culprit"] == "—"
