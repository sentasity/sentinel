"""Grouping, throttling, firing, and deadline enforcement."""

import logging
from unittest.mock import MagicMock

import pytest

from receiver.observability import DELIVERY_FAILURE_MARKER
from receiver.routines import FireOutcome
from receiver.sweep import (
    DEADLINE_REPLY,
    FIRED_ACK,
    MAX_ATTEMPTS,
    NOT_STARTED_PREFIX,
    NOT_STARTED_REASONS,
    RETRY_TTL_SECONDS,
    expire_overdue,
    fire_group,
    group_pending,
    run_sweep,
)


def row(issue_id, project="processing", release="a" * 40, environment="prod"):
    return {
        "issue_id": issue_id,
        "project": project,
        "release": release,
        "environment": environment,
        "conversation_id": f"conv-{issue_id}",
        "message_id": f"msg-{issue_id}",
        "short_id": f"PROCESSING-{issue_id}",
    }


def test_rows_sharing_project_release_and_environment_form_one_group():
    groups = group_pending([row("1"), row("2"), row("3")], max_batch=8)

    assert len(groups) == 1
    assert [r["issue_id"] for r in groups[0]] == ["1", "2", "3"]


def test_a_different_release_is_a_different_group():
    groups = group_pending([row("1"), row("2", release="b" * 40)], max_batch=8)

    assert len(groups) == 2


def test_a_different_environment_is_a_different_group():
    groups = group_pending([row("1"), row("2", environment="staging")], max_batch=8)

    assert len(groups) == 2


def test_a_different_project_is_a_different_group():
    groups = group_pending([row("1"), row("2", project="frontend")], max_batch=8)

    assert len(groups) == 2


def test_a_group_over_the_ceiling_splits_rather_than_firing_one_huge_session():
    groups = group_pending([row(str(i)) for i in range(10)], max_batch=4)

    assert [len(g) for g in groups] == [4, 4, 2]


def test_no_pending_rows_produce_no_groups():
    assert group_pending([], max_batch=8) == []


def collaborators(outcome=FireOutcome.FIRED, delay=0, claim=True, notice=True):
    store, routines, bot = MagicMock(), MagicMock(), MagicMock()
    store.claim_fire.return_value = claim
    store.claim_notice.return_value = notice
    store.hash_token.return_value = "hashed"
    routines.fire.return_value = (outcome, delay)
    return store, routines, bot


def conf():
    return MagicMock(daily_fire_cap=40, deadline_seconds=900)


def test_a_fired_group_moves_every_row_to_awaiting():
    store, routines, bot = collaborators()

    assert fire_group(
        [row("1"), row("2")], cfg=conf(), store=store, routines=routines, bot=bot
    ) == "fired"
    assert store.advance.call_count == 2
    assert store.advance.call_args.kwargs["due_at"]


def test_the_fire_payload_is_identifiers_only():
    """No free text: the payload must carry nothing an attacker influenced."""
    store, routines, bot = collaborators()

    fire_group([row("1"), row("2")], cfg=conf(), store=store, routines=routines, bot=bot)

    (payload,) = routines.fire.call_args.args
    assert set(payload) == {"project", "issue_ids", "release", "batch_id", "reply_token"}
    assert payload["issue_ids"] == ["1", "2"]


def test_the_reply_token_is_stored_hashed_never_in_the_clear():
    store, routines, bot = collaborators()

    fire_group([row("1")], cfg=conf(), store=store, routines=routines, bot=bot)

    (payload,) = routines.fire.call_args.args
    stored = store.advance.call_args.kwargs["extra"]
    assert stored["reply_token_hash"] == "hashed"
    assert payload["reply_token"] not in str(stored)


def test_a_group_is_not_fired_once_the_daily_cap_is_spent():
    store, routines, bot = collaborators(claim=False)

    assert fire_group(
        [row("1")], cfg=conf(), store=store, routines=routines, bot=bot
    ) == "throttled"
    routines.fire.assert_not_called()


def test_a_short_retry_after_is_the_daily_cap_and_is_retried():
    store, routines, bot = collaborators(FireOutcome.RATE_LIMITED, delay=3600)

    assert fire_group(
        [row("1")], cfg=conf(), store=store, routines=routines, bot=bot
    ) == "retry"
    assert RETRY_TTL_SECONDS >= 3600


def test_a_long_retry_after_is_window_exhaustion_and_is_not_retried():
    store, routines, bot = collaborators(FireOutcome.RATE_LIMITED, delay=RETRY_TTL_SECONDS + 1)

    assert fire_group(
        [row("1")], cfg=conf(), store=store, routines=routines, bot=bot
    ) == "exhausted"


def test_a_paused_routine_is_skipped_and_says_nothing_loud(caplog):
    """The operator's own budget kill switch must not read as an incident."""
    store, routines, bot = collaborators(FireOutcome.PAUSED)

    with caplog.at_level("WARNING"):
        assert fire_group(
            [row("1")], cfg=conf(), store=store, routines=routines, bot=bot
        ) == "paused"

    assert caplog.text == ""


def test_a_rejected_fire_marks_the_rows_failed():
    store, routines, bot = collaborators(FireOutcome.REJECTED)

    assert fire_group(
        [row("1")], cfg=conf(), store=store, routines=routines, bot=bot
    ) == "rejected"
    assert store.advance.call_args.args[-1] == "failed"


def test_a_fired_group_tells_every_thread_it_is_being_investigated():
    """The session takes minutes; the channel learns within one sweep."""
    store, routines, bot = collaborators()

    fire_group([row("1"), row("2")], cfg=conf(), store=store, routines=routines, bot=bot)

    posted = [c.args for c in bot.reply_in_thread.call_args_list]
    assert posted == [("conv-1", "msg-1", FIRED_ACK), ("conv-2", "msg-2", FIRED_ACK)]


def test_the_ack_is_posted_only_after_the_rows_are_durably_awaiting():
    """A post before the write would ack a fire the store never recorded."""
    store, routines, bot = collaborators()
    calls = []
    store.advance.side_effect = lambda *a, **k: calls.append("advance") or True
    bot.reply_in_thread.side_effect = lambda *a: calls.append("post")

    fire_group([row("1")], cfg=conf(), store=store, routines=routines, bot=bot)

    assert calls == ["advance", "post"]


def test_a_failed_ack_does_not_undo_the_fire(caplog):
    """The routine is already running: the ack is the only thing that lost."""
    store, routines, bot = collaborators()
    bot.reply_in_thread.side_effect = RuntimeError("bot down")

    with caplog.at_level("WARNING"):
        outcome = fire_group([row("1")], cfg=conf(), store=store, routines=routines, bot=bot)

    assert outcome == "fired"
    assert store.advance.call_args.args[3:] == ("pending", "fired")
    assert DELIVERY_FAILURE_MARKER not in caplog.text


def test_a_group_that_never_fired_tells_its_threads_why():
    store, routines, bot = collaborators(FireOutcome.REJECTED)

    fire_group([row("1")], cfg=conf(), store=store, routines=routines, bot=bot)

    conversation_id, message_id, text = bot.reply_in_thread.call_args.args
    assert (conversation_id, message_id) == ("conv-1", "msg-1")
    assert text == f"{NOT_STARTED_PREFIX}{NOT_STARTED_REASONS['rejected']}."


# Every way a fire attempt can end, as (collaborator kwargs, outcome name).
FIRE_PATHS = [
    ({"claim": False}, "throttled"),
    ({"outcome": FireOutcome.FIRED}, "fired"),
    ({"outcome": FireOutcome.PAUSED}, "paused"),
    ({"outcome": FireOutcome.RATE_LIMITED, "delay": 3600}, "retry"),
    ({"outcome": FireOutcome.RATE_LIMITED, "delay": RETRY_TTL_SECONDS + 1}, "exhausted"),
    ({"outcome": FireOutcome.RETRYABLE}, "retryable"),
    ({"outcome": FireOutcome.REJECTED}, "rejected"),
]


@pytest.mark.parametrize("kwargs,expected", FIRE_PATHS)
def test_every_path_a_fire_can_take_says_something_in_the_thread(kwargs, expected):
    """A new branch in `_fire` must not leave a thread silently unanswered."""
    store, routines, bot = collaborators(**kwargs)

    outcome = fire_group([row("1")], cfg=conf(), store=store, routines=routines, bot=bot)

    assert outcome == expected
    (posted,) = [c.args[-1] for c in bot.reply_in_thread.call_args_list]
    assert posted == (
        FIRED_ACK if expected == "fired"
        else f"{NOT_STARTED_PREFIX}{NOT_STARTED_REASONS[expected]}."
    )


def test_a_held_group_says_so_once_however_many_sweeps_hold_it():
    """A routine paused for a day must not post into the thread every minute."""
    store, routines, bot = collaborators(FireOutcome.PAUSED, notice=False)

    fire_group([row("1")], cfg=conf(), store=store, routines=routines, bot=bot)

    store.claim_notice.assert_called_once_with("1", "prod", "a" * 40, "paused")
    bot.reply_in_thread.assert_not_called()


def test_the_throttled_notice_names_the_budget_not_a_failure():
    store, routines, bot = collaborators(claim=False)

    fire_group([row("1")], cfg=conf(), store=store, routines=routines, bot=bot)

    assert bot.reply_in_thread.call_args.args[-1] == (
        f"{NOT_STARTED_PREFIX}{NOT_STARTED_REASONS['throttled']}."
    )


def awaiting(issue_id="1"):
    return {
        **row(issue_id),
        "conversation_id": "conv-1",
        "message_id": "msg-9",
        "short_id": "PROCESSING-7",
    }


def test_an_overdue_row_gets_a_fallback_reply_in_its_own_thread():
    store, bot = MagicMock(), MagicMock()
    store.query_due.return_value = [awaiting()]
    store.advance.return_value = True

    assert expire_overdue(store=store, bot=bot) == 1
    conversation_id, message_id, text = bot.reply_in_thread.call_args.args
    assert (conversation_id, message_id) == ("conv-1", "msg-9")
    assert text == DEADLINE_REPLY


def test_a_row_the_findings_handler_already_claimed_is_left_alone():
    """Whichever writer wins, the thread must never carry both messages."""
    store, bot = MagicMock(), MagicMock()
    store.query_due.return_value = [awaiting()]
    store.advance.return_value = False

    assert expire_overdue(store=store, bot=bot) == 0
    bot.reply_in_thread.assert_not_called()


def test_the_row_is_claimed_before_the_reply_is_posted():
    """Posting first would let both writers reply if the claim then failed."""
    store, bot = MagicMock(), MagicMock()
    store.query_due.return_value = [awaiting()]
    store.advance.return_value = True
    calls = []
    store.advance.side_effect = lambda *a, **k: calls.append("advance") or True
    bot.reply_in_thread.side_effect = lambda *a: calls.append("reply")

    expire_overdue(store=store, bot=bot)

    assert calls == ["advance", "reply"]


def test_a_failed_fallback_reply_is_logged_with_the_delivery_marker(caplog):
    store, bot = MagicMock(), MagicMock()
    store.query_due.return_value = [awaiting()]
    store.advance.return_value = True
    bot.reply_in_thread.side_effect = RuntimeError("bot down")

    with caplog.at_level("ERROR"):
        assert expire_overdue(store=store, bot=bot) == 0

    assert DELIVERY_FAILURE_MARKER in caplog.text


def test_the_fallback_claim_is_conditioned_on_no_stored_reply():
    """A stale index read must not land the fallback on a mid-retry row."""
    store, bot = MagicMock(), MagicMock()
    store.query_due.return_value = [awaiting()]
    store.advance.return_value = True

    expire_overdue(store=store, bot=bot)

    assert store.advance.call_args.kwargs["require_absent"] == "pending_reply"


def mid_retry(attempt=1):
    """An awaiting row whose findings arrived but whose reply failed to post."""
    return {
        **awaiting(),
        "pending_reply": "**PROCESSING-7** findings",
        "delivery_attempt": attempt,
    }


def test_an_overdue_mid_retry_row_reposts_its_findings_not_the_fallback():
    """The session DID report; "no findings are available" would be a lie."""
    store, bot = MagicMock(), MagicMock()
    store.query_due.return_value = [mid_retry()]
    store.advance.return_value = True

    expire_overdue(store=store, bot=bot)

    conversation_id, message_id, text = bot.reply_in_thread.call_args.args
    assert (conversation_id, message_id) == ("conv-1", "msg-9")
    assert text == "**PROCESSING-7** findings"
    assert store.advance.call_args.args[3:] == ("fired", "delivered")


def test_a_stored_card_reply_reposts_as_a_card_not_text():
    """Findings replies are serialized Adaptive Cards since the card renderer
    shipped; the plain-text branch above covers rows stored before it."""
    import json

    store, bot = MagicMock(), MagicMock()
    card = {"type": "AdaptiveCard", "body": []}
    store.query_due.return_value = [
        {**awaiting(), "pending_reply": json.dumps(card), "delivery_attempt": 1}
    ]
    store.advance.return_value = True

    expire_overdue(store=store, bot=bot)

    conversation_id, message_id, posted, summary = bot.reply_card_in_thread.call_args.args
    assert (conversation_id, message_id) == ("conv-1", "msg-9")
    assert posted == card
    assert summary == "Automated investigation: PROCESSING-7"
    bot.reply_in_thread.assert_not_called()


def test_a_mid_retry_row_another_writer_claimed_is_left_alone():
    store, bot = MagicMock(), MagicMock()
    store.query_due.return_value = [mid_retry()]
    store.advance.return_value = False

    expire_overdue(store=store, bot=bot)

    bot.reply_in_thread.assert_not_called()


def test_a_retry_that_fails_again_is_rescheduled_with_a_bumped_attempt():
    store, bot = MagicMock(), MagicMock()
    store.query_due.return_value = [mid_retry(attempt=1)]
    store.advance.return_value = True
    bot.reply_in_thread.side_effect = RuntimeError("bot down")

    expire_overdue(store=store, bot=bot)

    requeue = store.advance.call_args
    assert requeue.args[3:] == ("delivered", "fired")
    assert requeue.kwargs["due_at"]
    assert requeue.kwargs["extra"]["delivery_attempt"] == 2
    assert requeue.kwargs["extra"]["pending_reply"] == "**PROCESSING-7** findings"


def test_a_retry_at_the_attempt_ceiling_gives_up_with_the_marker(caplog):
    store, bot = MagicMock(), MagicMock()
    store.query_due.return_value = [mid_retry(attempt=MAX_ATTEMPTS - 1)]
    store.advance.return_value = True
    bot.reply_in_thread.side_effect = RuntimeError("bot down")

    with caplog.at_level("ERROR"):
        expire_overdue(store=store, bot=bot)

    assert DELIVERY_FAILURE_MARKER in caplog.text
    assert store.advance.call_args.args[3:] == ("delivered", "failed")


def shadow_cfg():
    return MagicMock(trigger_mode="shadow", max_batch_issues=8)


def pending_store(rows):
    """A store with no overdue awaiting rows and `rows` pending ones."""
    store = MagicMock()
    store.query_due.side_effect = lambda state, now, **_: rows if state == "pending" else []
    return store


def test_shadow_mode_groups_one_bad_deploy_into_one_investigation(caplog):
    """Six same-deploy rows must read as one shape, not an opaque row count."""
    store = pending_store([row(str(i)) for i in range(6)])

    with caplog.at_level("INFO"):
        summary = run_sweep(cfg=shadow_cfg(), store=store, routines=MagicMock(), bot=MagicMock())

    assert summary["shadow"]["rows"] == 6
    (shape,) = summary["shadow"]["groups"]
    assert shape["issue_ids"] == [str(i) for i in range(6)]
    assert shape["project"] == "processing"
    assert shape["environment"] == "prod"
    assert shape["release"] == "a" * 12
    assert "would investigate" in caplog.text
    # Validation A8 counts "sweep summary" lines as the liveness signal, so
    # the shadow branch must end on one just like the firing branch.
    assert "sweep summary" in caplog.text


def test_shadow_mode_reports_distinct_groups_separately():
    store = pending_store([row("1"), row("2", release="b" * 40)])

    summary = run_sweep(cfg=shadow_cfg(), store=store, routines=MagicMock(), bot=MagicMock())

    assert len(summary["shadow"]["groups"]) == 2


def test_shadow_mode_never_fires_or_spends_the_budget():
    store = pending_store([row(str(i)) for i in range(6)])
    routines = MagicMock()

    run_sweep(cfg=shadow_cfg(), store=store, routines=routines, bot=MagicMock())

    routines.fire.assert_not_called()
    store.claim_fire.assert_not_called()


def overdue_dispatch():
    return {
        "dispatch_id": "d-1",
        "short_id": "CHECKOUT-4B2",
        "conversation_id": "conv-1",
        "message_id": "msg-9",
        "status": "dispatched",
    }


def test_an_overdue_dispatch_is_failed_and_the_thread_told(caplog):
    from receiver.sweep import expire_autofix

    store = MagicMock()
    store.query_due.side_effect = lambda state, now, limit=50: (
        [overdue_dispatch()] if state == "autofix" else []
    )
    store.advance_autofix.return_value = True
    bot = MagicMock()

    assert expire_autofix(store=store, bot=bot) == 1
    assert "AUTOFIX_FAILED" in caplog.text
    reply = bot.reply_in_thread.call_args.args[2]
    assert "never reported back" in reply


def test_a_dispatch_the_callback_already_settled_is_left_alone():
    from receiver.sweep import expire_autofix

    store = MagicMock()
    store.query_due.side_effect = lambda state, now, limit=50: (
        [overdue_dispatch()] if state == "autofix" else []
    )
    store.advance_autofix.return_value = False
    bot = MagicMock()

    assert expire_autofix(store=store, bot=bot) == 0
    bot.reply_in_thread.assert_not_called()


def overdue_opening():
    return {**overdue_dispatch(), "status": "opening"}


def test_an_overdue_opening_claim_is_failed_with_the_receiver_reply(caplog):
    """A row at `opening` was claimed by a receiver that then never settled
    it, so the honest reply names the receiver, not the session."""
    from receiver.sweep import expire_autofix

    store = MagicMock()
    store.query_due.side_effect = lambda state, now, limit=50: (
        [overdue_opening()] if state == "autofix" else []
    )
    store.advance_autofix.return_value = True
    bot = MagicMock()

    assert expire_autofix(store=store, bot=bot) == 1
    assert store.advance_autofix.call_args.args == ("d-1", "opening", "failed")
    assert store.advance_autofix.call_args.kwargs == {"extra": {"failure": "opening timeout"}}
    assert "AUTOFIX_FAILED" in caplog.text
    reply = bot.reply_in_thread.call_args.args[2]
    assert "did not finish opening" in reply


def test_an_overdue_dispatch_still_expires_from_dispatched():
    from receiver.sweep import expire_autofix

    store = MagicMock()
    store.query_due.side_effect = lambda state, now, limit=50: (
        [overdue_dispatch()] if state == "autofix" else []
    )
    store.advance_autofix.return_value = True

    expire_autofix(store=store, bot=MagicMock())

    assert store.advance_autofix.call_args.args == ("d-1", "dispatched", "failed")
    assert store.advance_autofix.call_args.kwargs == {"extra": {"failure": "callback timeout"}}


def test_the_sweep_summary_counts_autofix_expiries():
    # Reuse the module's existing run_sweep fixtures/fakes; assert the key.
    from receiver.sweep import run_sweep

    store = MagicMock()
    store.query_due.return_value = []
    summary = run_sweep(cfg=MagicMock(trigger_mode="shadow", max_batch_issues=8),
                        store=store, routines=None, bot=MagicMock())

    assert "autofix_expired" in summary


# ---- Issue tracking: the backstop at grant expiry ----

from receiver.observability import ISSUE_TRACKING_FAILED_MARKER  # noqa: E402
from receiver.sweep import (  # noqa: E402
    TRACKING_UNFINISHED_NOTICE,
    TRACKING_UNTRACKED_NOTICE,
    expire_tracking,
)


def expired_grant(**overrides):
    grant = {
        "tracking_id": "t-1",
        "short_id": "CHECKOUT-4B2",
        "conversation_id": "conv-1",
        "message_id": "msg-9",
        "expect": "always",
        "status": "open",
    }
    grant.update(overrides)
    return grant


def tracking_store(grant, dispatch=None):
    store = MagicMock()
    store.query_due.side_effect = lambda state, now, limit=50: [grant] if state == "tracking" else []
    store.advance_tracking.return_value = True
    store.get_autofix_dispatch.return_value = dispatch
    return store


def test_the_notices_are_the_documented_text():
    assert TRACKING_UNTRACKED_NOTICE == (
        "⚠️ No GitHub issue was filed for this alert, so it is tracked only in this thread."
    )
    assert TRACKING_UNFINISHED_NOTICE == (
        "⚠️ Sentinel may not have finished filing this alert in GitHub; check the "
        "repository for an issue before filing one."
    )


def test_an_expired_grant_with_no_fix_attempt_is_untracked_and_the_thread_told(caplog):
    store, bot = tracking_store(expired_grant()), MagicMock()

    assert expire_tracking(store=store, bot=bot) == 1

    assert store.advance_tracking.call_args.args == ("t-1", "open", "untracked")
    assert bot.reply_in_thread.call_args.args == ("conv-1", "msg-9", TRACKING_UNTRACKED_NOTICE)
    assert f"{ISSUE_TRACKING_FAILED_MARKER} CHECKOUT-4B2" in caplog.text
    store.get_autofix_dispatch.assert_not_called()


@pytest.mark.parametrize(
    "dispatch",
    [
        {"status": "declined_in_session"},
        {"status": "not_reproducible"},
        {"status": "aborted_drift"},
        {"status": "failed", "failure": "session reported"},
    ],
)
def test_a_fix_attempt_the_session_ended_without_a_pr_gets_the_notice(dispatch):
    store = tracking_store(expired_grant(expect="unless_pr", dispatch_id="d-1"), dispatch)
    bot = MagicMock()

    assert expire_tracking(store=store, bot=bot) == 1

    assert store.get_autofix_dispatch.call_args.args == ("d-1",)
    assert store.advance_tracking.call_args.args == ("t-1", "open", "untracked")
    bot.reply_in_thread.assert_called_once()


@pytest.mark.parametrize(
    "dispatch",
    [
        {"status": "pr_opened", "pr_url": "https://x/pull/1"},
        {"status": "failed", "failure": "callback timeout"},
        {"status": "failed", "failure": "opening timeout"},
        {"status": "failed", "failure": "receiver crashed"},
        {"status": "failed", "failure": "pull request not opened"},
    ],
)
def test_a_pr_or_a_receiver_side_failure_releases_the_grant_silently(dispatch, caplog):
    """A PR means no issue; a receiver-side failure already has its own
    AUTOFIX_FAILED line and thread reply, and is never filed."""
    store = tracking_store(expired_grant(expect="unless_pr", dispatch_id="d-1"), dispatch)
    bot = MagicMock()

    assert expire_tracking(store=store, bot=bot) == 0

    assert store.advance_tracking.call_args.args == ("t-1", "open", "released")
    bot.reply_in_thread.assert_not_called()
    assert ISSUE_TRACKING_FAILED_MARKER not in caplog.text


@pytest.mark.parametrize("status", ["dispatched", "opening"])
def test_a_grant_whose_fix_is_still_running_waits_for_the_next_tick(status):
    store = tracking_store(
        expired_grant(expect="unless_pr", dispatch_id="d-1"), {"status": status}
    )
    bot = MagicMock()

    assert expire_tracking(store=store, bot=bot) == 0

    store.advance_tracking.assert_not_called()
    bot.reply_in_thread.assert_not_called()


def test_a_grant_whose_dispatch_cannot_be_read_gets_the_notice_not_silence():
    store = tracking_store(expired_grant(expect="unless_pr", dispatch_id="d-1"), None)
    bot = MagicMock()

    assert expire_tracking(store=store, bot=bot) == 1

    assert store.advance_tracking.call_args.args == ("t-1", "open", "untracked")


def test_an_op_that_died_mid_filing_gets_the_may_not_have_finished_notice():
    store = tracking_store(expired_grant(tracking_op="abc"))
    bot = MagicMock()

    expire_tracking(store=store, bot=bot)

    assert bot.reply_in_thread.call_args.args[2] == TRACKING_UNFINISHED_NOTICE


def test_a_failed_search_is_named_in_the_notice():
    """In the thread's own words: the issue-body line says Sentinel filed
    the issue, which contradicts a notice that nothing was filed."""
    from receiver.issues import SEARCH_UNAVAILABLE_LINE
    from receiver.sweep import TRACKING_SEARCH_UNAVAILABLE_NOTICE

    store = tracking_store(expired_grant(search_unavailable=True))
    bot = MagicMock()

    expire_tracking(store=store, bot=bot)

    text = bot.reply_in_thread.call_args.args[2]
    assert text == f"{TRACKING_UNTRACKED_NOTICE}\n\n{TRACKING_SEARCH_UNAVAILABLE_NOTICE}"
    assert TRACKING_SEARCH_UNAVAILABLE_NOTICE == (
        "The duplicate search was also unavailable for this alert, so an existing "
        "issue may already describe it."
    )
    assert SEARCH_UNAVAILABLE_LINE not in text


def test_a_grant_a_late_op_settled_first_gets_no_notice():
    store = tracking_store(expired_grant())
    store.advance_tracking.return_value = False
    bot = MagicMock()

    assert expire_tracking(store=store, bot=bot) == 0
    bot.reply_in_thread.assert_not_called()


def test_a_grant_no_longer_open_is_left_alone():
    store = tracking_store(expired_grant(status="tracked"))
    bot = MagicMock()

    assert expire_tracking(store=store, bot=bot) == 0
    store.advance_tracking.assert_not_called()


def test_a_failed_notice_is_logged_with_the_delivery_marker(caplog):
    store = tracking_store(expired_grant())
    bot = MagicMock()
    bot.reply_in_thread.side_effect = RuntimeError("teams down")

    assert expire_tracking(store=store, bot=bot) == 0
    assert f"{DELIVERY_FAILURE_MARKER} tracking notice for CHECKOUT-4B2" in caplog.text


def test_the_sweep_runs_the_tracking_backstop_every_tick():
    store = MagicMock()
    store.query_due.return_value = []

    summary = run_sweep(
        cfg=MagicMock(trigger_mode="shadow", max_batch_issues=8),
        store=store, routines=None, bot=MagicMock(),
    )

    assert summary["tracking_expired"] == 0
    assert "tracking" in [c.args[0] for c in store.query_due.call_args_list]


@pytest.mark.parametrize(
    "thread",
    [{"conversation_id": None}, {"message_id": None}, {"conversation_id": ""}],
    ids=["no-conversation", "no-message", "empty-conversation"],
)
def test_a_grant_with_no_thread_is_untracked_without_a_reply(thread, caplog):
    """A grant whose alert card never posted has no thread. The alert still
    ends untracked under the marker, but the notice is skipped with a warning,
    the way the /issues route skips its reply, rather than failing into a
    delivery-failure line for a thread that never existed."""
    grant = expired_grant()
    for key, value in thread.items():
        if value is None:
            del grant[key]
        else:
            grant[key] = value
    store, bot = tracking_store(grant), MagicMock()

    with caplog.at_level(logging.WARNING):
        assert expire_tracking(store=store, bot=bot) == 0

    assert store.advance_tracking.call_args.args == ("t-1", "open", "untracked")
    bot.reply_in_thread.assert_not_called()
    assert f"{ISSUE_TRACKING_FAILED_MARKER} CHECKOUT-4B2" in caplog.text
    assert "no thread to post the tracking notice in for CHECKOUT-4B2" in caplog.text
    assert DELIVERY_FAILURE_MARKER not in caplog.text
