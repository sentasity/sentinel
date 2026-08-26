"""The routines fire client."""

import json
from unittest.mock import MagicMock

from receiver.routines import (
    ANTHROPIC_VERSION,
    ROUTINE_BETA,
    TEXT_LIMIT,
    FireOutcome,
    UNCLASSIFIED_MARKER,
    RoutineClient,
    classify,
)


def make_client(response):
    client = RoutineClient("trig_abc", "tok-123")
    client.session = MagicMock()
    client.session.post.return_value = response
    return client


def ok(status=200, body="", headers=None):
    response = MagicMock()
    response.ok = 200 <= status < 300
    response.status_code = status
    response.text = body
    response.headers = headers or {}
    return response


def test_fire_posts_the_payload_as_a_json_string_in_text():
    client = make_client(ok())

    outcome, delay = client.fire({"project": "checkout", "issue_ids": ["1"]})

    assert outcome is FireOutcome.FIRED
    assert delay == 0
    (url,) = client.session.post.call_args.args
    assert url.endswith("/v1/claude_code/routines/trig_abc/fire")
    sent = client.session.post.call_args.kwargs["json"]
    assert set(sent) == {"text"}
    assert json.loads(sent["text"])["project"] == "checkout"


def test_fire_sends_both_required_version_headers():
    """The beta header is documented as required; a fire without it can 400."""
    client = make_client(ok())

    client.fire({"project": "checkout"})

    headers = client.session.post.call_args.kwargs["headers"]
    assert headers["anthropic-version"] == ANTHROPIC_VERSION
    assert headers["anthropic-beta"] == ROUTINE_BETA
    assert headers["Authorization"] == "Bearer tok-123"


def test_fire_refuses_a_payload_over_the_cap_without_calling_the_api():
    """Over the cap the API 400s with no truncation, so never send it."""
    client = make_client(ok())

    outcome, _ = client.fire({"project": "x" * (TEXT_LIMIT + 100)})

    assert outcome is FireOutcome.REJECTED
    client.session.post.assert_not_called()


def test_fire_treats_a_transport_error_as_retryable():
    client = RoutineClient("trig_abc", "tok-123")
    client.session = MagicMock()
    client.session.post.side_effect = OSError("connection reset")

    assert client.fire({"project": "x"})[0] is FireOutcome.RETRYABLE


# Two response shapes are unobserved: a paused routine's fire response, and a
# 429. The spike recorded that both the weekly subscription window and the
# documented per-account daily cap return `429 rate_limit_error` with
# `Retry-After`, so nothing distinguishes them by status code. PAUSED_MARKERS
# is therefore empty and every unrecognised failure stays loud until a real
# paused response is captured against the probe routine.


def test_a_429_is_rate_limited_and_carries_its_retry_delay():
    outcome, delay = classify(ok(429, '{"type":"error"}', {"Retry-After": "7200"}))

    assert outcome is FireOutcome.RATE_LIMITED
    assert delay == 7200


def test_a_429_without_a_usable_retry_after_still_classifies():
    assert classify(ok(429, "", {"Retry-After": "soon"})) == (FireOutcome.RATE_LIMITED, 0)
    assert classify(ok(429, "", {})) == (FireOutcome.RATE_LIMITED, 0)


def test_a_5xx_is_retryable():
    assert classify(ok(503))[0] is FireOutcome.RETRYABLE


def test_a_400_is_rejected_because_it_is_a_bug_not_a_budget_event():
    assert classify(ok(400, '{"error":{"message":"bad request"}}'))[0] is FireOutcome.REJECTED


def test_an_unrecognised_failure_is_never_silently_paused():
    """PAUSED is opt-in via PAUSED_MARKERS. Everything else stays loud."""
    for status in (400, 403, 409, 429, 500):
        assert classify(ok(status, "routine is paused"))[0] is not FireOutcome.PAUSED


def test_the_captured_paused_response_classifies_as_paused():
    """The real response, captured live 2026-08-17 against the paused probe
    routine (96-validation B8). This is the one failure allowed to be quiet."""
    body = (
        '{"error":{"message":"Routine is paused.","reason":"routine_paused",'
        '"type":"invalid_request_error"},'
        '"request_id":"req_011Ce929ZfZENqehkK3VMxGy","type":"error"}'
    )

    assert classify(ok(400, body)) == (FireOutcome.PAUSED, 0)


def test_a_marked_response_classifies_as_paused(monkeypatch):
    """Proves the mechanism works before a real marker is known."""
    monkeypatch.setattr("receiver.routines.PAUSED_MARKERS", ("routine_paused",))

    assert classify(ok(409, '{"error":{"type":"routine_paused"}}'))[0] is FireOutcome.PAUSED


def test_an_unclassified_failure_logs_its_raw_body_for_capture(caplog):
    """The first real 429 or pause must not need a reproduction to study."""
    with caplog.at_level("ERROR"):
        classify(ok(429, '{"type":"error","error":{"type":"rate_limit_error"}}'))

    assert UNCLASSIFIED_MARKER in caplog.text
    assert "rate_limit_error" in caplog.text


def test_a_rejected_fire_also_logs_its_body(caplog):
    with caplog.at_level("ERROR"):
        classify(ok(400, '{"error":{"message":"bad beta header"}}'))

    assert UNCLASSIFIED_MARKER in caplog.text
    assert "bad beta header" in caplog.text
