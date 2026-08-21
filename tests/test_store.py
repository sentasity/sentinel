"""DynamoDB persistence of posted alert cards."""

import copy
import re
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from receiver.models import parse_alert
from receiver.sentry_api import IssueRef
from receiver.store import AlertStore, BatchState
from tests.conftest import load_fixture

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def conditional_failure(operation: str) -> ClientError:
    """The error DynamoDB raises when a ConditionExpression is not met.

    A real botocore exception rather than a mock attribute: `except` against a
    MagicMock raises TypeError, so mocking the exception class cannot exercise
    the handler it is meant to test.
    """
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "failed"}},
        operation,
    )


def make_store():
    table = MagicMock()
    with patch("receiver.store.boto3.resource") as resource:
        resource.return_value.Table.return_value = table
        store = AlertStore("sentinel-alerts")
    return store, table


def test_put_alert_writes_the_expected_item():
    store, table = make_store()
    alert = parse_alert(load_fixture("sentry-webhook-alert.json"))

    store.put_alert(alert, IssueRef("SCANNERS-7X", "scanners"), "conv-1", "msg-9")

    item = table.put_item.call_args.kwargs["Item"]
    assert item["pk"] == "issue:1000000007"
    assert item["sk"] == "alert:staging"
    assert item["short_id"] == "SCANNERS-7X"
    assert item["project"] == "scanners"
    assert item["level"] == "error"
    assert item["conversation_id"] == "conv-1"
    assert item["message_id"] == "msg-9"
    assert item["release"] == "efa4bbfc4e79761e3542990fc090df1bc22ec47f"
    assert ISO_Z.match(item["posted_at"])
    assert item["last_alert_at"] == item["posted_at"]


def test_put_alert_omits_a_null_release():
    store, table = make_store()
    payload = copy.deepcopy(load_fixture("sentry-webhook-alert.json"))
    payload["data"]["event"]["release"] = None

    store.put_alert(parse_alert(payload), IssueRef("SCANNERS-7X", "scanners"), "c", "m")

    assert "release" not in table.put_item.call_args.kwargs["Item"]


def test_get_alert_reads_by_issue_and_environment():
    store, table = make_store()
    table.get_item.return_value = {"Item": {"message_id": "msg-9"}}

    assert store.get_alert("6543210987", "staging") == {"message_id": "msg-9"}
    assert table.get_item.call_args.kwargs["Key"] == {
        "pk": "issue:6543210987",
        "sk": "alert:staging",
    }


def test_get_alert_returns_none_when_absent():
    store, table = make_store()
    table.get_item.return_value = {}

    assert store.get_alert("6543210987", "staging") is None


def test_put_investigation_writes_a_sibling_item_under_the_same_partition():
    store, table = make_store()
    alert = parse_alert(load_fixture("sentry-webhook-alert.json"))

    store.put_investigation(
        alert, IssueRef("SCANNERS-7X", "scanners"), "conv-1", "msg-9", "2026-08-13T10:01:00Z"
    )

    item = table.put_item.call_args.kwargs["Item"]
    assert item["pk"] == "issue:1000000007"
    assert item["sk"] == "investigation:staging#efa4bbfc4e79761e3542990fc090df1bc22ec47f"
    assert item["status"] == "pending"
    assert item["due_pk"] == "pending"
    assert item["due_at"] == "2026-08-13T10:01:00Z"
    assert item["message_id"] == "msg-9"
    assert item["project"] == "scanners"


def test_put_investigation_guards_against_overwriting_an_existing_row():
    """The skip cache is this condition: one investigation per issue per release."""
    store, table = make_store()
    alert = parse_alert(load_fixture("sentry-webhook-alert.json"))

    assert store.put_investigation(
        alert, IssueRef("S", "scanners"), "c", "m", "2026-08-13T10:01:00Z"
    ) is True
    assert table.put_item.call_args.kwargs["ConditionExpression"] == "attribute_not_exists(pk)"


def test_put_investigation_returns_false_when_the_issue_was_already_enqueued():
    """A repeat alert on an unchanged release must not enqueue a second time."""
    store, table = make_store()
    table.put_item.side_effect = conditional_failure("PutItem")
    alert = parse_alert(load_fixture("sentry-webhook-alert.json"))

    assert store.put_investigation(
        alert, IssueRef("S", "scanners"), "c", "m", "2026-08-13T10:01:00Z"
    ) is False


def test_get_investigation_reads_by_issue_environment_and_release():
    store, table = make_store()
    table.get_item.return_value = {"Item": {"status": "fired"}}

    assert store.get_investigation("123", "prod", "abc") == {"status": "fired"}
    assert table.get_item.call_args.kwargs["Key"] == {
        "pk": "issue:123",
        "sk": "investigation:prod#abc",
    }


def test_query_due_reads_the_sparse_index_by_state_and_time():
    store, table = make_store()
    table.query.return_value = {"Items": [{"issue_id": "1"}]}

    rows = store.query_due("pending", "2026-08-13T10:05:00Z", limit=25)

    assert rows == [{"issue_id": "1"}]
    kwargs = table.query.call_args.kwargs
    assert kwargs["IndexName"] == "due-index"
    assert kwargs["Limit"] == 25
    assert kwargs["ExpressionAttributeValues"][":pk"] == "pending"
    assert kwargs["ExpressionAttributeValues"][":now"] == "2026-08-13T10:05:00Z"


def test_query_due_returns_an_empty_list_when_nothing_is_due():
    store, table = make_store()
    table.query.return_value = {}

    assert store.query_due("awaiting", "2026-08-13T10:05:00Z") == []


def test_advance_moves_a_row_and_keeps_it_in_the_index():
    store, table = make_store()

    store.advance("1", "prod", "abc", "pending", "fired", due_at="2026-08-13T10:15:00Z")

    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"]["sk"] == "investigation:prod#abc"
    assert kwargs["ExpressionAttributeValues"][":expected"] == "pending"
    assert kwargs["ExpressionAttributeValues"][":status"] == "fired"
    assert kwargs["ExpressionAttributeValues"][":due_pk"] == "awaiting"
    assert kwargs["ExpressionAttributeValues"][":due_at"] == "2026-08-13T10:15:00Z"


def test_advance_to_a_terminal_status_drops_the_row_out_of_the_index():
    store, table = make_store()

    store.advance("1", "prod", "abc", "fired", "delivered")

    assert "REMOVE due_pk, due_at" in table.update_item.call_args.kwargs["UpdateExpression"]


def test_advance_writes_extra_attributes_through_name_placeholders():
    """Raw attribute names risk colliding with DynamoDB reserved words."""
    store, table = make_store()

    store.advance(
        "1", "prod", "abc", "pending", "fired",
        due_at="2026-08-13T10:15:00Z",
        extra={"batch_id": "b-1", "reply_token_hash": "h-1"},
    )

    kwargs = table.update_item.call_args.kwargs
    assert set(kwargs["ExpressionAttributeNames"].values()) == {
        "status", "batch_id", "reply_token_hash",
    }
    assert "b-1" in kwargs["ExpressionAttributeValues"].values()
    assert "h-1" in kwargs["ExpressionAttributeValues"].values()


def test_advance_returns_false_when_another_writer_got_there_first():
    """The deadline sweep and the findings handler both target `awaiting`."""
    store, table = make_store()
    table.update_item.side_effect = conditional_failure("UpdateItem")

    assert store.advance("1", "prod", "abc", "awaiting", "delivered") is False


def test_advance_can_require_an_attribute_to_be_absent():
    """The deadline fallback must not claim a row the retry path re-armed."""
    store, table = make_store()

    store.advance("1", "prod", "abc", "fired", "failed", require_absent="pending_reply")

    kwargs = table.update_item.call_args.kwargs
    assert "attribute_not_exists(#absent)" in kwargs["ConditionExpression"]
    assert kwargs["ExpressionAttributeNames"]["#absent"] == "pending_reply"


def test_claim_fire_increments_a_single_counter_row():
    store, table = make_store()
    table.update_item.return_value = {"Attributes": {"fires": 3}}

    assert store.claim_fire("2026-08-13", cap=40) is True
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"pk": "fires:2026-08-13", "sk": "counter"}
    assert kwargs["ExpressionAttributeValues"][":cap"] == 40


def test_claim_fire_refuses_once_the_cap_is_reached():
    store, table = make_store()
    table.update_item.side_effect = conditional_failure("UpdateItem")

    assert store.claim_fire("2026-08-13", cap=40) is False


def test_claim_notice_writes_a_marker_the_next_sweep_will_see():
    store, table = make_store()

    assert store.claim_notice("1000000007", "prod", "abc", "paused") is True
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {
        "pk": "issue:1000000007",
        "sk": "investigation:prod#abc",
    }
    assert kwargs["ConditionExpression"] == "attribute_not_exists(#n)"
    assert ISO_Z.match(kwargs["ExpressionAttributeValues"][":now"])


def test_each_reason_claims_its_own_marker():
    """A row throttled this morning may still report a rejection tonight."""
    store, table = make_store()

    store.claim_notice("1000000007", "prod", "abc", "throttled")
    throttled = table.update_item.call_args.kwargs["ExpressionAttributeNames"]["#n"]
    store.claim_notice("1000000007", "prod", "abc", "rejected")
    rejected = table.update_item.call_args.kwargs["ExpressionAttributeNames"]["#n"]

    assert throttled != rejected


def test_claim_notice_refuses_a_row_that_already_posted_that_reason():
    store, table = make_store()
    table.update_item.side_effect = conditional_failure("UpdateItem")

    assert store.claim_notice("1000000007", "prod", "abc", "paused") is False


def live_row(issue_id="1"):
    return {"issue_id": issue_id, "status": "fired", "due_at": "2999-01-01T00:00:00Z"}


def test_claim_batch_looks_the_token_up_by_its_hash():
    store, table = make_store()
    table.query.return_value = {"Items": [live_row()]}

    state, rows = store.claim_batch("plain-token")

    assert state is BatchState.OPEN
    assert rows == [live_row()]
    kwargs = table.query.call_args.kwargs
    assert kwargs["IndexName"] == "token-index"
    assert kwargs["ExpressionAttributeValues"][":h"] == store.hash_token("plain-token")


def test_claim_batch_never_sends_the_plaintext_token_to_dynamo():
    store, table = make_store()
    table.query.return_value = {"Items": []}

    store.claim_batch("plain-token")

    assert "plain-token" not in str(table.query.call_args)


def test_claim_batch_returns_none_for_a_token_that_was_never_issued():
    store, table = make_store()
    table.query.return_value = {"Items": []}

    assert store.claim_batch("forged") == (BatchState.UNKNOWN, [])


def test_claim_batch_returns_empty_for_a_batch_already_delivered():
    """A session retrying its POST must not produce a second thread reply."""
    store, table = make_store()
    table.query.return_value = {"Items": [{"issue_id": "1", "status": "delivered"}]}

    assert store.claim_batch("plain-token") == (BatchState.DELIVERED, [])


def test_claim_batch_refuses_a_token_past_its_deadline():
    """The sweep only runs once a minute; the token must die on time anyway."""
    store, table = make_store()
    table.query.return_value = {
        "Items": [{"issue_id": "1", "status": "fired", "due_at": "2000-01-01T00:00:00Z"}]
    }

    assert store.claim_batch("plain-token") == (BatchState.EXPIRED, [])


def test_claim_batch_distinguishes_expired_from_delivered():
    """Both yield no rows, but one is a late session and one is a retry."""
    store, table = make_store()

    table.query.return_value = {
        "Items": [{"issue_id": "1", "status": "fired", "due_at": "2000-01-01T00:00:00Z"}]
    }
    expired, _ = store.claim_batch("t")
    table.query.return_value = {"Items": [{"issue_id": "1", "status": "delivered"}]}
    delivered, _ = store.claim_batch("t")

    assert expired is BatchState.EXPIRED
    assert delivered is BatchState.DELIVERED
    assert expired is not delivered


def test_claim_batch_drops_only_the_rows_that_expired():
    store, table = make_store()
    table.query.return_value = {
        "Items": [
            live_row("1"),
            {"issue_id": "2", "status": "fired", "due_at": "2000-01-01T00:00:00Z"},
        ]
    }

    state, rows = store.claim_batch("t")

    assert state is BatchState.OPEN
    assert [r["issue_id"] for r in rows] == ["1"]


def test_a_batch_the_sweep_already_expired_is_not_reported_as_delivered():
    """expire_overdue writes `failed` and strips due_at; that is not delivery."""
    store, table = make_store()
    table.query.return_value = {"Items": [{"issue_id": "1", "status": "failed"}]}

    assert store.claim_batch("t") == (BatchState.EXPIRED, [])


def test_expiry_does_not_depend_on_whether_the_sweep_ticked_first():
    """The same late POST must get the same answer either side of a sweep."""
    store, table = make_store()

    table.query.return_value = {
        "Items": [{"issue_id": "1", "status": "fired", "due_at": "2000-01-01T00:00:00Z"}]
    }
    before, _ = store.claim_batch("t")
    table.query.return_value = {"Items": [{"issue_id": "1", "status": "failed"}]}
    after, _ = store.claim_batch("t")

    assert before is after is BatchState.EXPIRED


def test_autofix_dedupe_claims_once_per_issue_and_release():
    store, table = make_store()

    assert store.claim_autofix_dedupe("1000000007", "staging", "79bad4b7" + "0" * 32)

    item = table.put_item.call_args.kwargs["Item"]
    assert item["pk"] == "issue:1000000007"
    assert item["sk"] == "autofix:staging#79bad4b7" + "0" * 32
    assert table.put_item.call_args.kwargs["ConditionExpression"] == (
        "attribute_not_exists(pk)"
    )


def test_a_second_autofix_claim_for_the_same_release_is_refused():
    store, table = make_store()
    table.put_item.side_effect = conditional_failure("PutItem")

    assert not store.claim_autofix_dedupe("1000000007", "staging", "abc")


def test_the_daily_pr_cap_is_a_conditional_counter():
    store, table = make_store()

    assert store.claim_autofix_pr("2026-08-17", 5)

    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"pk": "autofix-prs:2026-08-17", "sk": "counter"}
    assert kwargs["ExpressionAttributeValues"][":cap"] == 5


def test_a_spent_pr_cap_refuses_the_claim():
    store, table = make_store()
    table.update_item.side_effect = conditional_failure("UpdateItem")

    assert not store.claim_autofix_pr("2026-08-17", 5)


def test_a_dispatch_record_enters_the_due_index():
    store, table = make_store()

    store.put_autofix_dispatch(
        {
            "dispatch_id": "d-1",
            "issue_id": "1000000007",
            "environment": "staging",
            "release": "abc",
            "short_id": "SCANNERS-7X",
            "conversation_id": "conv-1",
            "message_id": "msg-9",
            "callback_token_hash": "hash",
        },
        due_at="2026-08-17T23:59:59Z",
    )

    item = table.put_item.call_args.kwargs["Item"]
    assert item["pk"] == "autofix:d-1"
    assert item["sk"] == "dispatch"
    assert item["status"] == "dispatched"
    assert item["due_pk"] == "autofix"
    assert item["due_at"] == "2026-08-17T23:59:59Z"


def test_advancing_a_dispatch_leaves_the_due_index():
    store, table = make_store()

    assert store.advance_autofix("d-1", "dispatched", "pr_opened", extra={"pr_url": "u"})

    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"pk": "autofix:d-1", "sk": "dispatch"}
    assert "REMOVE due_pk, due_at" in kwargs["UpdateExpression"]
    assert kwargs["ConditionExpression"] == "#s = :expected"


def test_an_already_settled_dispatch_refuses_a_second_advance():
    store, table = make_store()
    table.update_item.side_effect = conditional_failure("UpdateItem")

    assert not store.advance_autofix("d-1", "dispatched", "failed")
