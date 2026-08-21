"""Parsing of Sentry issue-alert webhook bodies."""

import copy

import pytest

from receiver.models import InvalidAlertPayload, SentryAlert, parse_alert
from tests.conftest import load_fixture


def test_parse_alert_extracts_core_fields():
    alert = parse_alert(load_fixture("sentry-webhook-alert.json"))

    assert isinstance(alert, SentryAlert)
    assert alert.issue_id == "1000000007"
    # Org-scoped, which is the shape Sentry actually sends. The receiver GETs
    # this URL verbatim, so the fixture carrying the wrong one would have hidden
    # a broken short-id lookup.
    assert alert.issue_api_url == (
        "https://sentry.io/api/0/organizations/example-org/issues/1000000007/"
    )
    assert alert.web_url.startswith(
        "https://sentry.io/organizations/example-org/issues/1000000007/"
    )
    assert alert.environment == "staging"
    assert alert.level == "error"
    assert alert.culprit == "__main__ in <module>"
    assert alert.release == "efa4bbfc4e79761e3542990fc090df1bc22ec47f"
    assert alert.rule_name == "[Backend API] New Issue - Staging"
    assert alert.title.startswith("AlertRelayMigrationSmokeTest")


def test_parse_alert_lowercases_level():
    payload = copy.deepcopy(load_fixture("sentry-webhook-alert.json"))
    payload["data"]["event"]["level"] = "WARNING"

    assert parse_alert(payload).level == "warning"


def test_parse_alert_rejects_untriggered_action():
    payload = copy.deepcopy(load_fixture("sentry-webhook-alert.json"))
    payload["action"] = "created"

    with pytest.raises(InvalidAlertPayload, match="unexpected action"):
        parse_alert(payload)


def test_parse_alert_rejects_event_without_issue_id():
    payload = copy.deepcopy(load_fixture("sentry-webhook-alert.json"))
    del payload["data"]["event"]["issue_id"]

    with pytest.raises(InvalidAlertPayload, match="issue_id"):
        parse_alert(payload)


def test_parse_alert_falls_back_to_environment_tag():
    payload = copy.deepcopy(load_fixture("sentry-webhook-alert.json"))
    del payload["data"]["event"]["environment"]
    payload["data"]["event"]["tags"] = [["environment", "prod"], ["level", "error"]]

    assert parse_alert(payload).environment == "prod"


def test_parse_alert_reads_dict_shaped_tags():
    payload = copy.deepcopy(load_fixture("sentry-webhook-alert.json"))
    del payload["data"]["event"]["environment"]
    payload["data"]["event"]["tags"] = [{"key": "environment", "value": "prod"}]

    assert parse_alert(payload).environment == "prod"


def test_parse_alert_rejects_event_with_no_environment_anywhere():
    payload = copy.deepcopy(load_fixture("sentry-webhook-alert.json"))
    del payload["data"]["event"]["environment"]
    payload["data"]["event"]["tags"] = [["level", "error"]]

    with pytest.raises(InvalidAlertPayload, match="no environment"):
        parse_alert(payload)


@pytest.mark.parametrize("payload", [None, [], "x", 1, True])
def test_parse_alert_rejects_a_non_object_body(payload):
    """A signed but non-object JSON body must not reach .get() and escape."""
    with pytest.raises(InvalidAlertPayload, match="not a JSON object"):
        parse_alert(payload)
