"""The eligibility gate."""

import copy
import logging
from unittest.mock import MagicMock

import pytest

from receiver.bot import BotError
from receiver.investigation import MAX_REQUEUES, SHA, eligible, enqueue_investigation
from receiver.models import parse_alert
from receiver.sentry_api import IssueRef
from receiver.sweep import NOT_STARTED_REASONS
from tests.conftest import load_fixture
from tests.test_handler import CONFIG


def alert_with(**changes):
    payload = copy.deepcopy(load_fixture("sentry-webhook-alert.json"))
    payload["data"]["event"].update(changes)
    return parse_alert(payload)


def test_an_error_with_a_release_is_eligible():
    assert eligible(alert_with(), CONFIG) == (True, "")


def test_a_warning_is_skipped_because_its_card_promised_so():
    ok, reason = eligible(alert_with(level="warning"), CONFIG)

    assert ok is False
    assert reason == "level"


def test_an_unserved_environment_is_skipped():
    assert eligible(alert_with(environment="dev"), CONFIG)[1] == "environment"


def test_a_missing_release_is_skipped_rather_than_investigated_at_head():
    assert eligible(alert_with(release=None), CONFIG)[1] == "no-release"


def test_a_release_that_is_not_a_sha_is_skipped():
    """`release_to_sha: identity` means the release must BE the commit."""
    assert eligible(alert_with(release="v1.2.3"), CONFIG)[1] == "release-not-a-sha"


def test_the_sha_pattern_accepts_exactly_forty_hex_characters():
    assert SHA.fullmatch("a" * 40)
    assert not SHA.fullmatch("a" * 39)
    assert not SHA.fullmatch("g" * 40)


# --- repeat alerts ---------------------------------------------------------

REF = IssueRef("CHECKOUT-4B2", "checkout")
NEW_CONVERSATION = "19:staging@thread.tacv2;messageid=200"
EARLIER_LINK = (
    "https://teams.microsoft.com/l/message/19%3Astaging%40thread.tacv2/100"
    "?tenantId=tenant-123&parentMessageId=100"
)


def existing(status, **extra):
    return {
        "status": status,
        "conversation_id": "19:staging@thread.tacv2;messageid=100",
        "message_id": "100",
        **extra,
    }


def enqueue(store, bot):
    enqueue_investigation(
        alert_with(), NEW_CONVERSATION, "200", cfg=CONFIG, ref=REF, store=store, bot=bot
    )


def test_a_first_alert_is_enqueued_and_its_thread_is_left_to_the_sweep():
    store, bot = MagicMock(), MagicMock()
    store.put_investigation.return_value = True

    enqueue(store, bot)

    store.put_investigation.assert_called_once()
    store.requeue_failed.assert_not_called()
    bot.reply_in_thread.assert_not_called()


def test_a_repeat_on_an_investigated_release_points_at_the_earlier_findings():
    store, bot = MagicMock(), MagicMock()
    store.put_investigation.return_value = False
    store.get_investigation.return_value = existing(
        "delivered", confidence="high", fixability="medium"
    )

    enqueue(store, bot)

    conversation_id, message_id, text = bot.reply_in_thread.call_args.args
    assert (conversation_id, message_id) == (NEW_CONVERSATION, "200")
    assert text.startswith("🔁 Not re-investigated: ")
    assert "efa4bbf" in text
    assert EARLIER_LINK in text
    assert "confidence high, fixability medium" in text
    store.requeue_failed.assert_not_called()


def test_a_pointer_omits_a_confidence_the_row_never_recorded():
    store, bot = MagicMock(), MagicMock()
    store.put_investigation.return_value = False
    store.get_investigation.return_value = existing("delivered")

    enqueue(store, bot)

    text = bot.reply_in_thread.call_args.args[2]
    assert EARLIER_LINK in text
    assert "confidence" not in text


@pytest.mark.parametrize("status", ["pending", "fired"])
def test_a_repeat_while_the_investigation_is_under_way_says_where_findings_will_land(status):
    store, bot = MagicMock(), MagicMock()
    store.put_investigation.return_value = False
    store.get_investigation.return_value = existing(status)

    enqueue(store, bot)

    text = bot.reply_in_thread.call_args.args[2]
    assert text.startswith("🔁 Not re-investigated: ")
    assert "under way" in text
    assert EARLIER_LINK in text
    store.requeue_failed.assert_not_called()


def test_a_repeat_after_a_failed_investigation_retries_it_under_the_new_card():
    store, bot = MagicMock(), MagicMock()
    store.put_investigation.return_value = False
    store.get_investigation.return_value = existing("failed")
    store.requeue_failed.return_value = True

    enqueue(store, bot)

    args, kwargs = store.requeue_failed.call_args
    assert args[:5] == (
        "1000000007", "staging", "efa4bbfc4e79761e3542990fc090df1bc22ec47f",
        NEW_CONVERSATION, "200",
    )
    assert kwargs["max_requeues"] == MAX_REQUEUES
    assert set(kwargs["notice_kinds"]) == set(NOT_STARTED_REASONS)
    # The sweep announces the retry when it fires; nothing to say yet.
    bot.reply_in_thread.assert_not_called()


def test_a_repeat_after_the_retry_budget_is_spent_says_so():
    store, bot = MagicMock(), MagicMock()
    store.put_investigation.return_value = False
    store.get_investigation.return_value = existing("failed")
    store.requeue_failed.return_value = False

    enqueue(store, bot)

    text = bot.reply_in_thread.call_args.args[2]
    assert text.startswith("🔁 Not re-investigated: ")
    assert "retried" in text
    assert EARLIER_LINK in text


def test_a_retry_another_repeat_already_claimed_points_at_that_repeats_card():
    """Two repeats seconds apart: the second loses the requeue and must link
    the thread the retry now belongs to, not the thread that failed."""
    store, bot = MagicMock(), MagicMock()
    store.put_investigation.return_value = False
    store.get_investigation.side_effect = [
        existing("failed"),
        existing("pending", conversation_id="19:staging@thread.tacv2;messageid=300",
                 message_id="300"),
    ]
    store.requeue_failed.return_value = False

    enqueue(store, bot)

    text = bot.reply_in_thread.call_args.args[2]
    assert "under way" in text
    assert "/19%3Astaging%40thread.tacv2/300?" in text
    assert "/100?" not in text


def test_a_repeat_whose_row_vanished_posts_nothing():
    """`--reset` on a replay deletes the row; a race with it is not an error."""
    store, bot = MagicMock(), MagicMock()
    store.put_investigation.return_value = False
    store.get_investigation.return_value = None

    enqueue(store, bot)

    bot.reply_in_thread.assert_not_called()
    store.requeue_failed.assert_not_called()


def test_a_pointer_that_cannot_be_posted_is_logged_not_raised(caplog):
    """The card is up and the row is settled; a chat blip costs one line."""
    store, bot = MagicMock(), MagicMock()
    store.put_investigation.return_value = False
    store.get_investigation.return_value = existing("delivered")
    bot.reply_in_thread.side_effect = BotError("teams down")

    with caplog.at_level(logging.WARNING, logger="receiver.investigation"):
        enqueue(store, bot)

    assert "CHECKOUT-4B2" in caplog.text
    assert "teams down" in caplog.text
