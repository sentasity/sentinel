#!/usr/bin/env python3
"""Post one test card through the live bot and reply in its thread.

    python -m scripts.bot_smoke --channel 19:...@thread.tacv2 [--level error]

Requires AWS credentials for the account holding the SSM parameters, and a
fully installed Teams app. Prints the conversation id and message id so the
thread-reply address can be checked by hand.
"""

from __future__ import annotations

import argparse
import sys

from receiver.bot import BotError, TeamsBotClient
from receiver.cards import card_summary, render_card
from receiver.config import assert_ready, get_secret, load_config
from scripts.preview_card import REF, build_alerts

SMOKE_REPLY = (
    "Smoke test reply. If this is threaded under the card above, "
    "the investigation engine's delivery path works."
)


def build_bot() -> TeamsBotClient:
    """Build the live bot client from config plus SSM."""
    cfg = load_config()
    assert_ready(cfg)
    return TeamsBotClient(
        tenant_id=cfg.tenant_id,
        app_id=cfg.bot_app_id,
        app_password=get_secret(cfg.secret_name("bot-client-secret")),
        service_url=cfg.service_url,
    )


def run(channel_id: str, level: str) -> int:
    """Post a card and thread a reply under it. Returns a process exit code."""
    alert = build_alerts()[level]
    card = render_card(alert, REF)
    summary = card_summary(alert, REF)
    bot = build_bot()
    try:
        conversation_id, message_id = bot.post_card(channel_id, card, summary)
        print(f"posted: conversation_id={conversation_id} message_id={message_id}")
        activity_id = bot.reply_in_thread(conversation_id, message_id, SMOKE_REPLY)
        print(f"replied: activity_id={activity_id}")
    except BotError as exc:
        print(f"bot call failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True, help="Teams channel id")
    parser.add_argument("--level", default="error", help="severity to render")
    args = parser.parse_args()
    raise SystemExit(run(args.channel, args.level))


if __name__ == "__main__":
    main()
