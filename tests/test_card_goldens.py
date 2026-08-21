"""The committed card gallery must match what the renderer produces."""

import json
from pathlib import Path

import pytest

from scripts.preview_card import SEVERITIES, build_cards

GALLERY = Path(__file__).resolve().parent.parent / "fixtures" / "cards"


@pytest.mark.parametrize("level", SEVERITIES)
def test_rendered_card_matches_the_committed_golden(level):
    golden = json.loads((GALLERY / f"{level}.json").read_text())

    assert build_cards()[level] == golden, (
        f"the {level} card changed. Re-run "
        f"`python scripts/preview_card.py --out fixtures/cards` and review the diff."
    )
