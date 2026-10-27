"""The offline card preview generator."""

import json

from scripts.preview_card import (
    FINDINGS,
    SEVERITIES,
    build_cards,
    build_reply_card,
    write_cards,
)


def test_build_cards_covers_every_reviewable_severity():
    cards = build_cards()

    assert list(cards) == list(SEVERITIES)
    assert all(card["type"] == "AdaptiveCard" for card in cards.values())


def test_write_cards_emits_the_whole_gallery(tmp_path):
    written = write_cards(tmp_path)

    assert sorted(p.name for p in written) == [
        "error.json",
        "fatal.json",
        "info.json",
        "reply.json",
        "warning.json",
    ]
    body = (tmp_path / "info.json").read_text()
    assert body.endswith("\n")
    assert json.loads(body)["body"][-1]["text"] == (
        "ℹ️ This kind of issue is not auto-investigated."
    )


def test_the_reply_card_preview_renders_the_schema_the_prompt_emits():
    """`evidence.mdx` shows this card, so it renders the v4 sample: urgency
    leads its status line and intent follows fixability, the way they do on a
    live findings reply."""
    assert FINDINGS.name == "findings-payload-v4.json"
    card = build_reply_card()
    assert card["body"][1]["text"] == (
        "Urgency: high · Confidence: high · Fixability: high · Intent: settled · "
        "Release: 79bad4b79fb0"
    )
    details = next(b for b in card["body"] if b.get("id") == "details")
    assert details["items"][0]["text"].startswith("**Intent settled by:** ")
