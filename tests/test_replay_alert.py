"""The alert replay script: payload construction, signing, and reset."""

import json
from unittest.mock import MagicMock

import pytest

from receiver.handler import signature_valid
from receiver.models import parse_alert
from scripts import replay_alert

STORED_ROW = {
    "pk": "issue:7690791246",
    "sk": "alert:prod",
    "short_id": "PROCESSING-49",
    "project": "processing",
    "level": "error",
    "title": "CloudWatch object count fetch failed for account 000000000000",
    "web_url": "https://sentry.io/organizations/acme-tools/issues/7690791246/",
    "release": "c2ed90e9a67748516780e30c7fd7f0306d954882",
    "conversation_id": "19:chan@thread.tacv2;messageid=1787634131941",
    "message_id": "1787634131941",
}


def build(row=None, org="acme-tools"):
    return replay_alert.build_payload(row or STORED_ROW, environment="prod", org=org)


def test_the_payload_parses_into_the_alert_the_row_describes():
    """The whole point: the receiver must read back what the row holds."""
    alert = parse_alert(build())

    assert alert.issue_id == "7690791246"
    assert alert.environment == "prod"
    assert alert.release == "c2ed90e9a67748516780e30c7fd7f0306d954882"
    assert alert.level == "error"
    assert alert.title == STORED_ROW["title"]
    assert alert.web_url == STORED_ROW["web_url"]


def test_the_payload_carries_a_resolvable_issue_api_url():
    """Without it resolve_issue_ref degrades to `#<numeric id>` and an empty
    project slug, and the autofix gate reads that project slug."""
    alert = parse_alert(build())

    assert alert.issue_api_url == (
        "https://sentry.io/api/0/organizations/acme-tools/issues/7690791246/"
    )


def test_the_environment_tag_agrees_with_the_environment_field():
    """parse_alert falls back to the tag list, so a stale tag left by the
    template would make a replay land in the wrong channel."""
    payload = build()
    event = payload["data"]["event"]

    assert event["environment"] == "prod"
    assert ["environment", "prod"] in [list(t) for t in event["tags"]]


def test_the_rule_name_carries_the_projects_prefix():
    """project_from_rule_name is the fallback path when the Sentry lookup fails."""
    payload = build()

    assert payload["data"]["triggered_rule"].startswith("[processing]")


def test_a_row_with_no_release_replays_as_a_null_release():
    alert = parse_alert(build({**STORED_ROW, "release": ""}))

    assert alert.release is None


def test_the_signature_the_script_sends_is_the_one_the_receiver_accepts():
    """One HMAC format, verified against the receiver's own check."""
    body = json.dumps(build())

    assert signature_valid(body, replay_alert.sign_body(body, "shh"), "shh")


def test_a_body_signed_with_another_secret_is_rejected():
    body = json.dumps(build())

    assert not signature_valid(body, replay_alert.sign_body(body, "wrong"), "shh")


def test_reset_deletes_the_two_rows_that_block_a_repeat():
    table = MagicMock()

    deleted = replay_alert.reset_rows(
        table, "7690791246", "prod", "c2ed90e9a67748516780e30c7fd7f0306d954882"
    )

    keys = [call.kwargs["Key"]["sk"] for call in table.delete_item.call_args_list]
    assert keys == [
        "investigation:prod#c2ed90e9a67748516780e30c7fd7f0306d954882",
        "autofix:prod#c2ed90e9a67748516780e30c7fd7f0306d954882",
    ]
    assert all(
        call.kwargs["Key"]["pk"] == "issue:7690791246"
        for call in table.delete_item.call_args_list
    )
    assert deleted == keys


def test_reset_never_touches_the_alert_row():
    """The alert row is the replay's own input; deleting it breaks the tool."""
    table = MagicMock()

    replay_alert.reset_rows(table, "7690791246", "prod", "abc")

    assert not any(
        call.kwargs["Key"]["sk"].startswith("alert:")
        for call in table.delete_item.call_args_list
    )


def test_reset_reports_a_row_that_was_not_there_rather_than_claiming_success():
    """An unconditional delete of a mistyped key succeeds silently; the
    condition is what makes an absent row distinguishable."""
    table = MagicMock()
    table.delete_item.side_effect = [
        replay_alert.conditional_error(),
        {"Attributes": {}},
    ]

    deleted = replay_alert.reset_rows(table, "7690791246", "prod", "abc")

    assert deleted == ["autofix:prod#abc"]
    for call in table.delete_item.call_args_list:
        assert call.kwargs["ConditionExpression"] == "attribute_exists(pk)"


def test_a_dynamodb_failure_other_than_a_missing_row_is_not_swallowed():
    from botocore.exceptions import ClientError

    table = MagicMock()
    table.delete_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "DeleteItem"
    )

    with pytest.raises(ClientError):
        replay_alert.reset_rows(table, "7690791246", "prod", "abc")


def test_the_sentry_route_is_derived_from_the_findings_url():
    assert (
        replay_alert.sentry_url("https://example.example.com/findings")
        == "https://example.example.com/sentry"
    )


def test_a_findings_url_with_a_deeper_path_keeps_its_prefix():
    assert (
        replay_alert.sentry_url("https://example.example.com/stage/findings")
        == "https://example.example.com/stage/sentry"
    )


def test_a_missing_alert_row_is_an_error_not_an_empty_replay():
    store = MagicMock()
    store.table.get_item.return_value = {}

    with pytest.raises(replay_alert.ReplayError, match="no alert row"):
        replay_alert.load_row(store, "7690791246", "prod")
