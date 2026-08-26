"""The offline card preview generator."""

import json

from scripts.preview_card import SEVERITIES, build_cards, write_cards


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
    body = (tmp_path / "warning.json").read_text()
    assert body.endswith("\n")
    assert json.loads(body)["body"][-1]["text"] == (
        "⚠️ Warnings are not auto-investigated."
    )
