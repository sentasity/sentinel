#!/usr/bin/env python3
"""Render one Adaptive Card per severity for offline review.

    python scripts/preview_card.py [--out fixtures/cards]

Paste any output file into https://adaptivecards.io/designer to see exactly
what the channel will show. No AWS or Teams credentials involved.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from receiver.cards import render_card
from receiver.models import parse_alert
from receiver.sentry_api import IssueRef

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "fixtures" / "sentry-webhook-alert.json"
DEFAULT_OUT = REPO_ROOT / "fixtures" / "cards"

SEVERITIES = ("fatal", "error", "warning", "info")
REF = IssueRef(short_id="SCANNERS-7X", project="scanners")


def build_alerts() -> dict[str, object]:
    """Parse the sample webhook body once per reviewable severity."""
    base = json.loads(FIXTURE.read_text())
    alerts = {}
    for level in SEVERITIES:
        payload = copy.deepcopy(base)
        payload["data"]["event"]["level"] = level
        alerts[level] = parse_alert(payload)
    return alerts


def build_cards() -> dict[str, dict]:
    """Render the sample alert at every reviewable severity."""
    return {level: render_card(alert, REF) for level, alert in build_alerts().items()}


def write_cards(out_dir: Path) -> list[Path]:
    """Write each rendered card as pretty JSON. Returns the written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for level, card in build_cards().items():
        path = out_dir / f"{level}.json"
        path.write_text(json.dumps(card, indent=2, ensure_ascii=False) + "\n")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    for path in write_cards(args.out):
        print(path)


if __name__ == "__main__":
    main()
