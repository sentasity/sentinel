"""The live-bot smoke script's orchestration (the bot itself is mocked)."""

from unittest.mock import MagicMock, patch

from scripts import bot_smoke


def test_smoke_posts_a_card_then_replies_in_its_thread(capsys):
    bot = MagicMock()
    bot.post_card.return_value = ("conv-1", "msg-9")
    bot.reply_in_thread.return_value = "act-2"

    with patch.object(bot_smoke, "build_bot", return_value=bot):
        exit_code = bot_smoke.run("19:chan@thread.tacv2", "error")

    assert exit_code == 0
    channel, card, summary = bot.post_card.call_args.args
    assert channel == "19:chan@thread.tacv2"
    assert card["type"] == "AdaptiveCard"
    assert summary.startswith("🔴 ERROR")
    bot.reply_in_thread.assert_called_once()
    assert bot.reply_in_thread.call_args.args[:2] == ("conv-1", "msg-9")

    out = capsys.readouterr().out
    assert "conversation_id=conv-1" in out
    assert "message_id=msg-9" in out


def test_smoke_reports_a_failed_post_as_a_nonzero_exit(capsys):
    from receiver.bot import BotError

    bot = MagicMock()
    bot.post_card.side_effect = BotError("conversation create failed: HTTP 403")

    with patch.object(bot_smoke, "build_bot", return_value=bot):
        exit_code = bot_smoke.run("19:chan@thread.tacv2", "error")

    assert exit_code == 1
    assert "conversation create failed" in capsys.readouterr().err
