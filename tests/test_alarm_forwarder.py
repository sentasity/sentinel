"""The alarm forwarder: CloudWatch alarm notifications to an ntfy topic."""

import json
from unittest.mock import MagicMock
from urllib.error import URLError

import pytest

from receiver import alarm_forwarder

DESCRIPTION = (
    "An investigation's session never reported back before its deadline. "
    "The thread got the fallback reply."
)


def alarm(state="ALARM"):
    return {
        "AlarmName": "sentinel-receiver-ReceiverInvestigationTimedOut-ABC123",
        "AlarmDescription": DESCRIPTION,
        "NewStateValue": state,
        "StateChangeTime": "2026-01-01T12:00:00.000+0000",
    }


def sns_event(*messages):
    return {"Records": [{"Sns": {"Message": m}} for m in messages]}


@pytest.fixture(autouse=True)
def forwarder_env(monkeypatch):
    monkeypatch.setenv("NTFY_SERVER", "https://ntfy.example.com")
    monkeypatch.setenv("NTFY_TOPIC_PARAMETER", "/sentinel/ntfy-topic")
    monkeypatch.setattr(alarm_forwarder, "_topic", None)


@pytest.fixture
def ssm(monkeypatch):
    client = MagicMock()
    client.get_parameter.return_value = {"Parameter": {"Value": "topic-abc"}}
    monkeypatch.setattr(alarm_forwarder, "_ssm_client", lambda: client)
    return client


@pytest.fixture
def urlopen(monkeypatch):
    opener = MagicMock()
    monkeypatch.setattr(alarm_forwarder.urllib.request, "urlopen", opener)
    return opener


def test_alarm_record_builds_a_high_priority_message():
    assert alarm_forwarder.build_message(alarm(), "topic-abc") == {
        "topic": "topic-abc",
        "title": "Sentinel: An investigation's session never reported back before its deadline",
        "message": (
            f"{DESCRIPTION}\n\n"
            "Alarm: sentinel-receiver-ReceiverInvestigationTimedOut-ABC123\n"
            "At: 2026-01-01T12:00:00.000+0000"
        ),
        "priority": 4,
        "tags": ["sentinel"],
    }


@pytest.mark.parametrize("state", ["OK", "INSUFFICIENT_DATA"])
def test_ok_and_insufficient_data_records_are_skipped(state):
    assert alarm_forwarder.build_message(alarm(state), "topic-abc") is None


def test_handler_publishes_each_alarm_record(ssm, urlopen):
    event = sns_event(json.dumps(alarm()), json.dumps(alarm("OK")))

    assert alarm_forwarder.lambda_handler(event, None) == {"published": 1, "skipped": 1}

    request = urlopen.call_args.args[0]
    assert request.full_url == "https://ntfy.example.com"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == alarm_forwarder.build_message(alarm(), "topic-abc")
    assert urlopen.call_args.kwargs["timeout"] == 10


def test_unparsable_sns_message_is_skipped_not_raised(ssm, urlopen):
    assert alarm_forwarder.lambda_handler(sns_event("not json"), None) == {
        "published": 0, "skipped": 1,
    }
    urlopen.assert_not_called()


def test_failed_post_raises(ssm, urlopen):
    urlopen.side_effect = URLError("unreachable")

    with pytest.raises(URLError):
        alarm_forwarder.lambda_handler(sns_event(json.dumps(alarm())), None)


def test_topic_is_fetched_once_per_container(ssm, urlopen):
    event = sns_event(json.dumps(alarm()))

    alarm_forwarder.lambda_handler(event, None)
    alarm_forwarder.lambda_handler(event, None)

    ssm.get_parameter.assert_called_once_with(Name="/sentinel/ntfy-topic", WithDecryption=True)


def test_the_forwarder_never_imports_the_receiver_stack():
    """It runs with 128 MB and no receiver config: the handler module's
    imports (Sentry, requests, the config loader) must stay out of it."""
    source = alarm_forwarder.__loader__.get_source(alarm_forwarder.__name__)

    assert "from receiver" not in source
    assert "import receiver" not in source
