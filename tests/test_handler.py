"""Function URL routing, HMAC verification, and the alert pipeline."""

import base64
import copy
import hashlib
import hmac
import json
import logging
from unittest.mock import MagicMock, call, patch

import pytest

from receiver import handler, observability
from receiver.config import ReceiverConfig
from receiver.github_app import PullRequestOutcome
from receiver.store import BatchState
from tests.conftest import load_fixture

SECRET = "webhook-secret"

CONFIG = ReceiverConfig(
    environments=("prod", "staging"),
    account="123456789012",
    region="us-east-1",
    table_name="sentinel-alerts",
    alarm_email="ops@example.com",
    tenant_id="tenant-123",
    service_url="https://smba.trafficmanager.net/amer/",
    bot_app_id="app-456",
    channels={"prod": "19:prod@thread.tacv2", "staging": "19:staging@thread.tacv2"},
    sentry_org="acme-tools",
    ssm_prefix="/sentinel",
    automation_dsn="",
)


def sign(body: str) -> str:
    return hmac.new(SECRET.encode(), body.encode("utf-8"), hashlib.sha256).hexdigest()


def request(method="POST", path="/sentry", body=None, signature=None, b64=False):
    raw = body if body is not None else json.dumps(load_fixture("sentry-webhook-alert.json"))
    headers = {"content-type": "application/json"}
    if signature is not None:
        headers["sentry-hook-signature"] = signature
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": headers,
        "body": base64.b64encode(raw.encode()).decode() if b64 else raw,
        "isBase64Encoded": b64,
    }


def test_health_route_returns_200():
    assert handler.lambda_handler(request(method="GET", path="/health"), None)["statusCode"] == 200


def test_bot_route_returns_200_and_ignores_the_body():
    response = handler.lambda_handler(request(path="/bot", body="{}"), None)

    assert response["statusCode"] == 200


def test_unknown_route_returns_404():
    assert handler.lambda_handler(request(path="/nope", body="{}"), None)["statusCode"] == 404


def test_missing_signature_is_rejected():
    with patch.object(handler, "webhook_secret", return_value=SECRET):
        response = handler.lambda_handler(request(), None)

    assert response["statusCode"] == 401


def test_wrong_signature_is_rejected():
    with patch.object(handler, "webhook_secret", return_value=SECRET):
        response = handler.lambda_handler(request(signature="deadbeef"), None)

    assert response["statusCode"] == 401


def test_non_allowlisted_environment_is_dropped():
    payload = copy.deepcopy(load_fixture("sentry-webhook-alert.json"))
    payload["data"]["event"]["environment"] = "dev"
    payload["data"]["event"]["tags"] = [["environment", "dev"]]
    raw = json.dumps(payload)

    with patch.object(handler, "webhook_secret", return_value=SECRET), \
         patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "deliver") as deliver:
        response = handler.lambda_handler(request(body=raw, signature=sign(raw)), None)

    assert response["statusCode"] == 204
    deliver.assert_not_called()


def test_base64_encoded_body_is_verified_against_the_decoded_bytes():
    raw = json.dumps(load_fixture("sentry-webhook-alert.json"))

    with patch.object(handler, "webhook_secret", return_value=SECRET), \
         patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "deliver", return_value=None):
        response = handler.lambda_handler(
            request(body=raw, signature=sign(raw), b64=True), None
        )

    assert response["statusCode"] == 200


from receiver.bot import BotError
from receiver.sentry_api import IssueRef
from receiver.sweep import MAX_ATTEMPTS


def wired(bot=None, store=None, ref=IssueRef("CHECKOUT-4B2", "checkout")):
    """Patch every collaborator `deliver` reaches out to."""
    return (
        patch.object(handler, "webhook_secret", return_value=SECRET),
        patch.object(handler, "config", return_value=CONFIG),
        patch.object(handler, "bot_client", return_value=bot or MagicMock()),
        patch.object(handler, "alert_store", return_value=store or MagicMock()),
        patch.object(handler, "resolve_issue_ref", return_value=ref),
        patch.object(handler, "get_secret", return_value="tok"),
    )


def test_error_alert_is_rendered_posted_and_stored():
    bot, store = MagicMock(), MagicMock()
    bot.post_card.return_value = ("conv-1", "msg-9")
    raw = json.dumps(load_fixture("sentry-webhook-alert.json"))

    patches = wired(bot=bot, store=store)
    for p in patches:
        p.start()
    try:
        response = handler.lambda_handler(request(body=raw, signature=sign(raw)), None)
    finally:
        for p in patches:
            p.stop()

    assert response["statusCode"] == 200
    channel, card, summary = bot.post_card.call_args.args
    assert channel == "19:staging@thread.tacv2"
    assert card["type"] == "AdaptiveCard"
    assert summary.startswith("🔴 ERROR CHECKOUT-4B2: ")
    store.put_alert.assert_called_once()
    assert store.put_alert.call_args.args[2:] == ("conv-1", "msg-9")


def test_bot_failure_returns_500_so_sentry_sees_the_truth():
    bot = MagicMock()
    bot.post_card.side_effect = BotError("conversation create failed: HTTP 502")
    raw = json.dumps(load_fixture("sentry-webhook-alert.json"))

    patches = wired(bot=bot)
    for p in patches:
        p.start()
    try:
        response = handler.lambda_handler(request(body=raw, signature=sign(raw)), None)
    finally:
        for p in patches:
            p.stop()

    assert response["statusCode"] == 500


def test_bot_failure_logs_the_marker_the_alarm_watches(caplog):
    """The 500 is invisible to CloudWatch: the invocation itself succeeds.

    Lambda's Errors metric counts failed invocations, and returning a 500
    response body is a successful one. This log line is the only signal the
    delivery-failure alarm has, so losing it makes the alarm silent.
    """
    bot = MagicMock()
    bot.post_card.side_effect = BotError("conversation create failed: HTTP 502")
    raw = json.dumps(load_fixture("sentry-webhook-alert.json"))

    patches = wired(bot=bot)
    for p in patches:
        p.start()
    try:
        with caplog.at_level(logging.ERROR):
            handler.lambda_handler(request(body=raw, signature=sign(raw)), None)
    finally:
        for p in patches:
            p.stop()

    assert observability.DELIVERY_FAILURE_MARKER in caplog.text


def test_the_handler_flushes_sentry_on_the_delivery_failure_path():
    """The failure path is the one whose event matters most, and it is queued.

    Lambda freezes the environment when the handler returns, so the event the
    LOG.error creates never reaches Sentry unless the handler drains first.
    """
    bot = MagicMock()
    bot.post_card.side_effect = BotError("conversation create failed: HTTP 502")
    raw = json.dumps(load_fixture("sentry-webhook-alert.json"))

    patches = wired(bot=bot)
    for p in patches:
        p.start()
    try:
        with patch.object(handler, "flush_sentry") as flush:
            handler.lambda_handler(request(body=raw, signature=sign(raw)), None)
    finally:
        for p in patches:
            p.stop()

    flush.assert_called_once()


def test_the_handler_flushes_sentry_even_on_an_unrouted_request():
    with patch.object(handler, "flush_sentry") as flush:
        handler.lambda_handler({"rawPath": "/nope"}, None)

    flush.assert_called_once()


def test_the_handler_flushes_sentry_even_when_the_pipeline_raises():
    """An unhandled exception is exactly when the event must not be lost."""
    patches = wired()
    for p in patches:
        p.start()
    try:
        with patch.object(handler, "handle_sentry", side_effect=RuntimeError("boom")):
            with patch.object(handler, "flush_sentry") as flush:
                with pytest.raises(RuntimeError):
                    handler.lambda_handler(
                        {"rawPath": "/sentry", "requestContext": {"http": {"method": "POST"}}},
                        None,
                    )
    finally:
        for p in patches:
            p.stop()

    flush.assert_called_once()


def test_warning_alert_posts_to_prod_channel_without_a_footer():
    payload = copy.deepcopy(load_fixture("sentry-webhook-alert.json"))
    payload["data"]["event"]["environment"] = "prod"
    payload["data"]["event"]["level"] = "warning"
    raw = json.dumps(payload)
    bot = MagicMock()
    bot.post_card.return_value = ("conv-2", "msg-10")

    patches = wired(bot=bot)
    for p in patches:
        p.start()
    try:
        response = handler.lambda_handler(request(body=raw, signature=sign(raw)), None)
    finally:
        for p in patches:
            p.stop()

    assert response["statusCode"] == 200
    channel, card, _ = bot.post_card.call_args.args
    assert channel == "19:prod@thread.tacv2"
    assert card["body"][-1]["type"] == "FactSet"


# The Function URL is unauthenticated at the platform, so every one of these
# reaches the handler from the open internet. None of them may escape as an
# uncaught exception: the error alarm fires on a single Lambda error.


def test_non_ascii_signature_header_is_rejected_not_crashed():
    with patch.object(handler, "webhook_secret", return_value=SECRET):
        response = handler.lambda_handler(request(signature="деadbeef"), None)

    assert response["statusCode"] == 401


def test_malformed_base64_body_returns_400():
    with patch.object(handler, "webhook_secret", return_value=SECRET):
        event = request(signature="deadbeef")
        event["body"] = "!!!not-base64!!!"
        event["isBase64Encoded"] = True
        response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 400


def test_non_utf8_body_returns_400():
    with patch.object(handler, "webhook_secret", return_value=SECRET):
        event = request(signature="deadbeef")
        event["body"] = base64.b64encode(b"\xff\xfe\x00").decode()
        event["isBase64Encoded"] = True
        response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 400


def test_a_delivered_alert_is_enqueued_for_investigation():
    bot, store = MagicMock(), MagicMock()
    bot.post_card.return_value = ("conv-1", "msg-9")
    raw = json.dumps(load_fixture("sentry-webhook-alert.json"))

    patches = wired(bot=bot, store=store)
    for p in patches:
        p.start()
    try:
        with patch.object(handler, "enqueue_investigation") as enqueue:
            handler.lambda_handler(request(body=raw, signature=sign(raw)), None)
    finally:
        for p in patches:
            p.stop()

    enqueue.assert_called_once()
    assert enqueue.call_args.args[1:] == ("conv-1", "msg-9")
    # The bot goes along so a repeat can answer the card it just posted.
    assert enqueue.call_args.kwargs["bot"] is bot


def test_an_enqueue_failure_never_turns_into_a_sentry_retry():
    """A 500 makes Sentry resend, and the card already posted: two cards."""
    bot, store = MagicMock(), MagicMock()
    bot.post_card.return_value = ("conv-1", "msg-9")
    raw = json.dumps(load_fixture("sentry-webhook-alert.json"))

    patches = wired(bot=bot, store=store)
    for p in patches:
        p.start()
    try:
        with patch.object(
            handler, "enqueue_investigation", side_effect=RuntimeError("dynamo down")
        ):
            response = handler.lambda_handler(request(body=raw, signature=sign(raw)), None)
    finally:
        for p in patches:
            p.stop()

    assert response["statusCode"] == 200


def test_an_eventbridge_event_runs_the_sweep_not_the_url_router():
    """A scheduled event has no rawPath, so it must be recognised first."""
    with patch.object(handler, "sweep", return_value={"fired": 0}) as sweep:
        result = handler.lambda_handler(
            {"source": "aws.events", "detail-type": "Scheduled Event"}, None
        )

    sweep.assert_called_once()
    assert result == {"fired": 0}


def test_a_function_url_request_never_runs_the_sweep():
    with patch.object(handler, "sweep") as sweep:
        handler.lambda_handler(request(method="GET", path="/health"), None)

    sweep.assert_not_called()


def test_a_shadow_sweep_never_builds_the_routine_client():
    """Shadow must stay inert without the firing secrets: the trigger-token
    parameter is a rollout prerequisite that lands after the first deploy, and
    an eager fetch crashed every sweep tick until it existed."""
    store, bot = MagicMock(), MagicMock()
    store.query_due.return_value = []

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "alert_store", return_value=store), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "routine_client") as routines:
        summary = handler.sweep()

    routines.assert_not_called()
    assert summary["shadow"] == {"rows": 0, "groups": []}


FINDINGS_BODY = json.dumps({"schema_version": 1, "batch_id": "b1", "results": []})


def findings_request(token=None, body=FINDINGS_BODY):
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return {
        "rawPath": "/findings",
        "requestContext": {"http": {"method": "POST", "path": "/findings"}},
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }


def test_findings_without_a_token_is_401():
    with patch.object(handler, "config", return_value=CONFIG):
        assert handler.lambda_handler(findings_request(), None)["statusCode"] == 401


def test_findings_with_a_non_ascii_token_is_rejected_not_crashed():
    with patch.object(handler, "config", return_value=CONFIG):
        assert handler.lambda_handler(findings_request("tökén"), None)["statusCode"] == 401


def test_findings_with_an_unknown_token_is_401():
    store = MagicMock()
    store.claim_batch.return_value = (BatchState.UNKNOWN, [])

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "alert_store", return_value=store):
        assert handler.lambda_handler(findings_request("nope"), None)["statusCode"] == 401


def test_findings_for_an_already_delivered_batch_is_200_with_no_repost():
    """A session retrying its POST must not double-post the thread reply.

    Every 200 from this route carries the same JSON shape, because the
    session parses the body rather than reading a status code: a plain-text
    "ok" here would be a parse error on exactly the retry path.
    """
    store = MagicMock()
    store.claim_batch.return_value = (BatchState.DELIVERED, [])

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "alert_store", return_value=store), \
         patch.object(handler, "deliver_findings") as deliver:
        response = handler.lambda_handler(findings_request("tok"), None)

    assert response["statusCode"] == 200
    assert response["headers"]["content-type"] == "application/json"
    assert json.loads(response["body"]) == {"autofix": None, "tracking": None}
    deliver.assert_not_called()


def awaiting_row(issue_id="1000000007"):
    return {
        "issue_id": issue_id,
        "environment": "staging",
        "release": "efa4bbfc4e79761e3542990fc090df1bc22ec47f",
        "batch_id": "6f1d2c88-0a2b-4f77-9d31-8f0d6a7c1e42",
        "conversation_id": "conv-1",
        "message_id": "msg-9",
        "short_id": "CHECKOUT-4B2",
    }


def test_each_result_replies_in_its_own_card_thread():
    bot, store = MagicMock(), MagicMock()
    store.advance.return_value = True

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        response = handler.deliver_findings(
            load_fixture("findings-payload.json"), [awaiting_row()]
        )

    assert response["statusCode"] == 200
    conversation_id, message_id, card, summary = bot.reply_card_in_thread.call_args.args
    assert (conversation_id, message_id) == ("conv-1", "msg-9")
    assert summary == "Automated investigation: CHECKOUT-4B2"
    assert "CHECKOUT-4B2" in card["body"][0]["text"]


def test_an_invalid_document_is_400_and_leaves_the_row_awaiting():
    """Leaving it awaiting is what lets the deadline still answer the thread."""
    bot, store = MagicMock(), MagicMock()

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        response = handler.deliver_findings(
            load_fixture("findings-payload-invalid.json"), [awaiting_row()]
        )

    assert response["statusCode"] == 400
    bot.reply_card_in_thread.assert_not_called()
    store.advance.assert_not_called()


def test_a_successful_reply_ends_delivered_and_posts_exactly_once():
    bot, store = MagicMock(), MagicMock()
    store.advance.return_value = True

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        handler.deliver_findings(load_fixture("findings-payload.json"), [awaiting_row()])

    bot.reply_card_in_thread.assert_called_once()
    store.advance.assert_called_once()
    assert store.advance.call_args.args[3:] == ("fired", "delivered")


def test_a_delivered_finding_records_its_confidence_for_repeat_alerts():
    """A later alert on the same release quotes the row, not the thread."""
    bot, store = MagicMock(), MagicMock()
    store.advance.return_value = True

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        handler.deliver_findings(load_fixture("findings-payload.json"), [awaiting_row()])

    assert store.advance.call_args.kwargs["extra"] == {"confidence": "high"}


def findings_v3(urgency="high"):
    """The v2 fixture as a schema v3 document: what the shipped prompt sends."""
    payload = copy.deepcopy(load_fixture("findings-payload-v2.json"))
    payload["schema_version"] = 3
    payload["results"][0]["urgency"] = urgency
    return payload


def test_a_delivered_v3_finding_records_its_urgency_for_repeat_alerts():
    bot, store = MagicMock(), MagicMock()
    store.advance.return_value = True

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        handler.deliver_findings(findings_v3(), [awaiting_row()])

    assert store.advance.call_args.kwargs["extra"] == {
        "confidence": "high",
        "fixability": "high",
        "urgency": "high",
    }


def test_a_delivered_v4_finding_records_its_intent_for_repeat_alerts():
    bot, store = MagicMock(), MagicMock()
    store.advance.return_value = True

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        handler.deliver_findings(load_fixture("findings-payload-v4.json"), [awaiting_row()])

    assert store.advance.call_args.kwargs["extra"] == {
        "confidence": "high",
        "fixability": "high",
        "urgency": "high",
        "intent": "settled",
    }


@pytest.mark.parametrize(
    "fixture, version",
    [
        ("findings-payload.json", 1),
        ("findings-payload-v2.json", 2),
        ("findings-payload-v3.json", 3),
    ],
)
def test_an_outdated_schema_is_still_delivered_and_logs_its_marker(caplog, fixture, version):
    """After the prompt moves to v4, an older document can only come from a
    routine still running an older prompt. It keeps its findings: the card
    posts as usual, and the marker makes the stale prompt findable."""
    bot, store = MagicMock(), MagicMock()
    store.advance.return_value = True

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        with caplog.at_level(logging.WARNING):
            response = handler.deliver_findings(load_fixture(fixture), [awaiting_row()])

    assert response["statusCode"] == 200
    bot.reply_card_in_thread.assert_called_once()
    assert (
        f"{observability.FINDINGS_SCHEMA_OUTDATED_MARKER} schema_version {version}"
        in caplog.text
    )
    assert observability.FINDINGS_REJECTED_MARKER not in caplog.text


def test_a_current_schema_logs_no_outdated_marker(caplog):
    bot, store = MagicMock(), MagicMock()
    store.advance.return_value = True

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        with caplog.at_level(logging.WARNING):
            handler.deliver_findings(
                load_fixture("findings-payload-v4.json"), [awaiting_row()]
            )

    assert observability.FINDINGS_SCHEMA_OUTDATED_MARKER not in caplog.text


def test_a_failed_reply_is_requeued_for_the_sweep_not_lost():
    """`delivered` is terminal, so stopping at a log line would drop findings
    that already passed validation and redaction."""
    bot, store = MagicMock(), MagicMock()
    store.advance.return_value = True
    bot.reply_card_in_thread.side_effect = BotError("reply failed: HTTP 502")

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        response = handler.deliver_findings(
            load_fixture("findings-payload.json"), [awaiting_row()]
        )

    assert response["statusCode"] == 200
    requeue = store.advance.call_args
    assert requeue.args[3:] == ("delivered", "fired")
    assert requeue.kwargs["due_at"]
    assert requeue.kwargs["extra"]["delivery_attempt"] == 1
    # The stored reply is the serialized card, so the sweep reposts what the
    # first attempt composed.
    stored = json.loads(requeue.kwargs["extra"]["pending_reply"])
    assert stored["type"] == "AdaptiveCard"
    assert "CHECKOUT-4B2" in stored["body"][0]["text"]


def test_a_transient_reply_failure_does_not_trip_the_delivery_alarm(caplog):
    """The marker means a reply is lost. A blip the next sweep absorbs is not
    that, and logging it would fire the alarm on every retried success."""
    bot, store = MagicMock(), MagicMock()
    store.advance.return_value = True
    bot.reply_card_in_thread.side_effect = BotError("reply failed: HTTP 502")

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        with caplog.at_level(logging.ERROR):
            handler.deliver_findings(load_fixture("findings-payload.json"), [awaiting_row()])

    assert observability.DELIVERY_FAILURE_MARKER not in caplog.text


def test_a_reply_that_exhausts_its_attempts_is_failed_with_the_marker(caplog):
    bot, store = MagicMock(), MagicMock()
    store.advance.return_value = True
    bot.reply_card_in_thread.side_effect = BotError("reply failed: HTTP 502")
    row = {**awaiting_row(), "delivery_attempt": MAX_ATTEMPTS - 1}

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        with caplog.at_level(logging.ERROR):
            handler.deliver_findings(load_fixture("findings-payload.json"), [row])

    assert observability.DELIVERY_FAILURE_MARKER in caplog.text
    assert store.advance.call_args.args[3:] == ("delivered", "failed")


def test_a_row_the_deadline_already_claimed_is_not_replied_to_twice():
    bot, store = MagicMock(), MagicMock()
    store.advance.return_value = False

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        handler.deliver_findings(load_fixture("findings-payload.json"), [awaiting_row()])

    bot.reply_card_in_thread.assert_not_called()


def test_a_rejected_document_logs_the_marker_its_alarm_watches(caplog):
    bot, store = MagicMock(), MagicMock()

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "bot_client", return_value=bot), \
         patch.object(handler, "alert_store", return_value=store):
        with caplog.at_level(logging.ERROR):
            handler.deliver_findings(
                load_fixture("findings-payload-invalid.json"), [awaiting_row()]
            )

    assert observability.FINDINGS_REJECTED_MARKER in caplog.text


def test_findings_past_the_deadline_is_401_not_a_quiet_ok():
    """An expired token is dead; answering 200 would look like success."""
    store = MagicMock()
    store.claim_batch.return_value = (BatchState.EXPIRED, [])

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "alert_store", return_value=store), \
         patch.object(handler, "deliver_findings") as deliver:
        response = handler.lambda_handler(findings_request("tok"), None)

    assert response["statusCode"] == 401
    deliver.assert_not_called()


def probe_request(
    body='{"schema_version": 2, "repo_remote": "https://github.com/acme-tools/checkout", "health_status": 200}',
    path="/findings/probe",
):
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": {"content-type": "application/json"},
        "body": body,
        "isBase64Encoded": False,
    }


def test_the_probe_endpoint_is_actually_routed():
    """The probe prompt posts here; without this route it 404s in silence."""
    response = handler.lambda_handler(probe_request(), None)

    assert response["statusCode"] == 200


def test_a_probe_report_is_logged_where_the_rollout_gate_can_read_it(caplog):
    with caplog.at_level(logging.INFO):
        handler.lambda_handler(probe_request(), None)

    assert observability.PROBE_MARKER in caplog.text
    assert "acme-tools/checkout" in caplog.text


def test_a_probe_report_is_truncated_so_it_cannot_flood_the_log(caplog):
    """The endpoint is unauthenticated, so its log volume must be bounded."""
    with caplog.at_level(logging.INFO):
        handler.lambda_handler(probe_request(body="x" * 50_000), None)

    assert len(caplog.text) < 10_000


def test_the_probe_route_is_reached_before_the_findings_route():
    """Exact-equality routing: the specific path must be checked first."""
    with patch.object(handler, "handle_findings") as findings:
        handler.lambda_handler(probe_request(), None)

    findings.assert_not_called()


from receiver.handler import deliver_findings


def autofix_row(issue_id="1000000007", short_id="CHECKOUT-4B2", message_id="msg-9"):
    return {
        "issue_id": issue_id,
        "environment": "staging",
        "release": "79bad4b79fb044dc6386fa690aae2bc3a6ebcc29",
        "batch_id": "6f1d2c88-0a2b-4f77-9d31-8f0d6a7c1e42",
        "project": "checkout",
        "short_id": short_id,
        "conversation_id": "conv-1",
        "message_id": message_id,
    }


SECOND_ISSUE_ID = "1000000011"
SECOND_SHORT_ID = "CHECKOUT-9C1"


def two_passing_results():
    """The v4 fixture with a second passing result in the same project.

    Every other autofix test runs a one-result batch, which cannot tell a
    reply loop that keeps going after one row fails from one that stops.
    """
    payload = copy.deepcopy(load_fixture("findings-payload-v4.json"))
    second = copy.deepcopy(payload["results"][0])
    second["issue_id"] = SECOND_ISSUE_ID
    second["short_id"] = SECOND_SHORT_ID
    second["root_cause"] = (
        "reserve_stock returns before the ledger write commits, so a cancelled "
        "order leaves the reservation held."
    )
    second["evidence"] = [
        {
            "file": "src/checkout/services/inventory.py",
            "symbol": "reserve_stock",
            "line": 92,
            "note": "the commit happens after the early return",
        }
    ]
    second["next_step"] = "commit the ledger write before reserve_stock returns"
    payload["results"].append(second)
    return payload


def two_autofix_rows():
    return [
        autofix_row(),
        autofix_row(
            issue_id=SECOND_ISSUE_ID, short_id=SECOND_SHORT_ID, message_id="msg-10"
        ),
    ]


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_passing_finding_returns_a_grant_and_says_so_in_the_thread(
    config, alert_store, bot_client, github_client, tmp_path
):
    from receiver.store import AlertStore
    from tests.test_config import AUTOFIX, VALID, write
    from receiver.config import load_config

    cfg = load_config(write(tmp_path, VALID + AUTOFIX))
    config.return_value = cfg
    store = alert_store.return_value
    store.advance.return_value = True
    store.claim_autofix_dedupe.return_value = True
    store.claim_autofix_pr.return_value = True

    response = deliver_findings(load_fixture("findings-payload-v4.json"), [autofix_row()])

    assert response["statusCode"] == 200
    assert response["headers"]["content-type"] == "application/json"
    block = json.loads(response["body"])["autofix"]
    # The key set is a contract with the session prompt, which reads these
    # names verbatim, so pin the whole shape rather than the fields in use.
    # No token is among them: the session never authenticates to GitHub,
    # and the receiver mints its own when the fix comes back.
    assert set(block) == {"repo", "base_branch", "callback_url", "grants"}
    assert block["repo"] == cfg.target_repo
    assert block["base_branch"] == cfg.autofix_base_branch
    assert block["callback_url"] == cfg.autofix_callback_url
    github_client.return_value.mint_autofix_token.assert_not_called()

    grant = block["grants"][0]
    assert set(grant) == {
        "issue_id",
        "short_id",
        "dispatch_id",
        "callback_token",
        "cited_files",
    }
    assert grant["issue_id"] == autofix_row()["issue_id"]
    assert grant["short_id"] == "CHECKOUT-4B2"
    assert grant["dispatch_id"]
    assert grant["callback_token"]

    record = store.put_autofix_dispatch.call_args.args[0]
    assert record["dispatch_id"] == grant["dispatch_id"]
    assert record["callback_token_hash"] == AlertStore.hash_token(grant["callback_token"])
    # Only the hash is stored: the token itself lives in the response body
    # and nowhere else, so a table read cannot replay a callback.
    assert grant["callback_token"] not in str(record)

    card = bot_client.return_value.reply_card_in_thread.call_args.args[2]
    assert "attempting a fix in this session" in json.dumps(card)


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_declined_finding_returns_a_null_autofix_block(
    config, alert_store, bot_client, github_client, tmp_path
):
    from tests.test_config import AUTOFIX, VALID, write
    from receiver.config import load_config

    cfg = load_config(
        write(tmp_path, VALID + AUTOFIX.replace("- checkout", "- frontend"))
    )
    # The substitution above is what drives the decline, and a no-op replace
    # would leave this test quietly exercising the opted-in config instead.
    assert cfg.autofix_projects == ("frontend",)
    config.return_value = cfg
    store = alert_store.return_value
    store.advance.return_value = True

    response = deliver_findings(load_fixture("findings-payload-v4.json"), [autofix_row()])

    assert json.loads(response["body"]) == {"autofix": None, "tracking": None}
    card = bot_client.return_value.reply_card_in_thread.call_args.args[2]
    assert "Autofix declined: project not opted in." in json.dumps(card)
    github_client.return_value.mint_autofix_token.assert_not_called()
    store.put_autofix_dispatch.assert_not_called()


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_finding_from_before_intent_is_delivered_but_earns_no_grant(
    config, alert_store, bot_client, github_client, caplog, tmp_path
):
    """Between the receiver deploy and the prompt update, a routine still
    sends v3. Its findings post as usual; the gate fails closed and the
    thread and the log both say why."""
    from tests.test_config import AUTOFIX, VALID, write
    from receiver.config import load_config

    config.return_value = load_config(write(tmp_path, VALID + AUTOFIX))
    store = alert_store.return_value
    store.advance.return_value = True
    store.claim_autofix_dedupe.return_value = True
    store.claim_autofix_pr.return_value = True

    with caplog.at_level(logging.INFO):
        response = deliver_findings(
            load_fixture("findings-payload-v3.json"), [autofix_row()]
        )

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["autofix"] is None
    card = bot_client.return_value.reply_card_in_thread.call_args.args[2]
    assert "Autofix declined: intent not rated." in json.dumps(card)
    assert (
        f"{observability.AUTOFIX_DECLINED_MARKER} CHECKOUT-4B2 reason=intent not rated"
        in caplog.text
    )
    store.claim_autofix_dedupe.assert_not_called()
    store.put_autofix_dispatch.assert_not_called()


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_failed_thread_post_still_returns_the_grant(
    config, alert_store, bot_client, github_client, tmp_path
):
    """A chat outage is not a reason to abandon the fix: the findings survived
    validation, and the grant has already been staged."""
    from tests.test_config import AUTOFIX, VALID, write
    from receiver.config import load_config

    config.return_value = load_config(write(tmp_path, VALID + AUTOFIX))
    store = alert_store.return_value
    store.advance.return_value = True
    store.claim_autofix_dedupe.return_value = True
    store.claim_autofix_pr.return_value = True
    bot_client.return_value.reply_card_in_thread.side_effect = BotError("teams down")

    response = deliver_findings(load_fixture("findings-payload-v4.json"), [autofix_row()])

    block = json.loads(response["body"])["autofix"]
    assert len(block["grants"]) == 1
    store.advance_autofix.assert_not_called()


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_non_bot_error_on_one_row_still_answers_the_next_one(
    config, alert_store, bot_client, github_client, tmp_path, caplog
):
    """`BotError` is not the only thing this loop can raise. The chat client
    is built on first use in the container and reads its credential to do so,
    so a cold container meeting a throttled parameter store fails here; and
    the retry the `BotError` path schedules is itself a conditional write that
    re-raises anything but the condition failing. Every row is already
    `delivered` by the time the loop runs, which is terminal and out of the
    due index, so a guard that only understands `BotError` would leave every
    row after the failing one silent forever. The grants are pending in the
    response too, so losing the loop would cost the session its fix as well
    as the remaining threads their replies."""
    from tests.test_config import AUTOFIX, VALID, write
    from receiver.config import load_config

    config.return_value = load_config(write(tmp_path, VALID + AUTOFIX))
    store = alert_store.return_value
    store.advance.return_value = True
    store.claim_autofix_dedupe.return_value = True
    store.claim_autofix_pr.return_value = True
    bot_client.return_value.reply_card_in_thread.side_effect = [
        RuntimeError("parameter store unreachable"),
        None,
    ]

    with caplog.at_level(logging.ERROR):
        response = deliver_findings(two_passing_results(), two_autofix_rows())

    posts = bot_client.return_value.reply_card_in_thread.call_args_list
    assert len(posts) == 2, "the first row's failure stopped the loop"
    assert posts[1].args[1] == "msg-10"
    block = json.loads(response["body"])["autofix"]
    assert [g["short_id"] for g in block["grants"]] == ["CHECKOUT-4B2", SECOND_SHORT_ID]
    assert observability.DELIVERY_FAILURE_MARKER in caplog.text
    # The generic path logs and moves on rather than scheduling a retry: it
    # cannot know the card survived rendering, so it has nothing to store for
    # the sweep to send. Only the `BotError` path returns a row to `fired`.
    transitions = [c.args[3:5] for c in store.advance.call_args_list]
    assert transitions == [("fired", "delivered"), ("fired", "delivered")]


@patch("receiver.handler.render_reply_card")
@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_card_render_that_raises_costs_only_its_own_row(
    config, alert_store, bot_client, github_client, render, tmp_path, caplog
):
    """Rendering is not exception-free by contract, and it happens before the
    post, so a row can be lost without the chat client ever being reached.
    The row after it must still get its reply."""
    from tests.test_config import AUTOFIX, VALID, write
    from receiver.config import load_config

    config.return_value = load_config(write(tmp_path, VALID + AUTOFIX))
    store = alert_store.return_value
    store.advance.return_value = True
    store.claim_autofix_dedupe.return_value = True
    store.claim_autofix_pr.return_value = True
    render.side_effect = [ValueError("unrenderable finding"), ({"card": "ok"}, 0)]

    with caplog.at_level(logging.ERROR):
        response = deliver_findings(two_passing_results(), two_autofix_rows())

    posts = bot_client.return_value.reply_card_in_thread.call_args_list
    assert len(posts) == 1, "the surviving row never got its reply"
    assert posts[0].args[1] == "msg-10"
    block = json.loads(response["body"])["autofix"]
    assert len(block["grants"]) == 2
    assert observability.DELIVERY_FAILURE_MARKER in caplog.text


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_disabled_gate_leaves_the_reply_untouched(
    config, alert_store, bot_client, github_client, tmp_path
):
    from tests.test_config import VALID, write
    from receiver.config import load_config

    config.return_value = load_config(write(tmp_path, VALID))
    store = alert_store.return_value
    store.advance.return_value = True

    response = deliver_findings(load_fixture("findings-payload-v4.json"), [autofix_row()])

    card = bot_client.return_value.reply_card_in_thread.call_args.args[2]
    assert "Autofix" not in json.dumps(card)
    assert json.loads(response["body"]) == {"autofix": None, "tracking": None}
    github_client.return_value.mint_autofix_token.assert_not_called()


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_gate_crash_never_costs_the_findings_reply(
    config, alert_store, bot_client, github_client, tmp_path, caplog
):
    """The gate must never cost the reply that already survived validation
    and redaction, mirroring test_an_enqueue_failure_never_turns_into_a_sentry_retry
    for the sibling guard around enqueue_investigation."""
    from tests.test_config import AUTOFIX, VALID, write
    from receiver.config import load_config

    config.return_value = load_config(write(tmp_path, VALID + AUTOFIX))
    store = alert_store.return_value
    store.advance.return_value = True
    store.claim_autofix_dedupe.return_value = True
    store.claim_autofix_pr.return_value = True
    store.put_autofix_dispatch.side_effect = RuntimeError("dynamo down")

    with caplog.at_level(logging.ERROR):
        response = deliver_findings(load_fixture("findings-payload-v4.json"), [autofix_row()])

    assert response["statusCode"] == 200
    bot_client.return_value.reply_card_in_thread.assert_called_once()
    card = bot_client.return_value.reply_card_in_thread.call_args.args[2]
    assert "Autofix" not in json.dumps(card)
    assert json.loads(response["body"]) == {"autofix": None, "tracking": None}
    github_client.return_value.mint_autofix_token.assert_not_called()
    assert observability.AUTOFIX_FAILED_MARKER in caplog.text


# ---- Issue tracking: the grant at findings time ----

from receiver.issues import GRANT_FIELDS, TRACKING_CAPS, TRACKING_FIELDS


def tracking_cfg(tmp_path, *, autofix=False, environments="[staging]"):
    """A config with issue filing on; the rows these tests deliver are staging."""
    from receiver.config import load_config
    from tests.test_config import AUTOFIX, ISSUES, VALID, write

    issues_block = ISSUES.replace("environments: [staging]", f"environments: {environments}")
    return load_config(write(tmp_path, VALID + (AUTOFIX if autofix else "") + issues_block))


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_tracking_grants_one_entry_per_delivered_result(
    config, alert_store, bot_client, github_client, tmp_path
):
    from receiver.store import AlertStore

    config.return_value = tracking_cfg(tmp_path)
    store = alert_store.return_value
    store.advance.return_value = True

    response = deliver_findings(two_passing_results(), two_autofix_rows())

    body = json.loads(response["body"])
    assert body["autofix"] is None
    block = body["tracking"]
    # The key sets are a contract with the investigator prompt, which reads
    # these names verbatim, so the whole shape is pinned.
    assert set(block) == set(TRACKING_FIELDS)
    assert block["repo"] == "acme-tools/checkout"
    assert block["issues_url"] == "https://example.test/issues"
    assert block["issue_types"] == ["Bug", "Task"]
    assert block["fields"] == ["Priority", "Effort"]
    assert block["project"] is True
    assert block["caps"] == TRACKING_CAPS
    assert [g["short_id"] for g in block["grants"]] == ["CHECKOUT-4B2", SECOND_SHORT_ID]
    records = [c.args[0] for c in store.put_tracking_grant.call_args_list]
    assert len(records) == 2
    for record, grant, row in zip(records, block["grants"], two_autofix_rows()):
        assert set(grant) == set(GRANT_FIELDS)
        assert grant["issue_id"] == row["issue_id"]
        assert record["tracking_id"] == grant["tracking_id"]
        assert record["expires_at"] == grant["expires_at"]
        # Only the hash is stored, as for the autofix callback token.
        assert record["token_hash"] == AlertStore.hash_token(grant["tracking_token"])
        assert grant["tracking_token"] not in str(record)
        assert record["expect"] == "always"
        assert "dispatch_id" not in record
        for key in ("issue_id", "environment", "release", "conversation_id", "message_id"):
            assert record[key] == row[key]
        assert record["short_id"] == row["short_id"]


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_tracking_gives_a_granted_result_the_fix_phase_and_then_its_window(
    config, alert_store, bot_client, github_client, tmp_path
):
    from datetime import datetime

    def at(stamp: str) -> datetime:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))

    config.return_value = tracking_cfg(tmp_path, autofix=True)
    store = alert_store.return_value
    store.advance.return_value = True
    store.claim_autofix_dedupe.return_value = True
    store.claim_autofix_pr.return_value = True

    before = handler._deadline(3600 + 1800)
    response = deliver_findings(load_fixture("findings-payload-v4.json"), [autofix_row()])
    after = handler._deadline(3600 + 1800)

    body = json.loads(response["body"])
    record = store.put_tracking_grant.call_args.args[0]
    assert record["expect"] == "unless_pr"
    assert record["dispatch_id"] == body["autofix"]["grants"][0]["dispatch_id"]
    assert at(before) <= at(record["expires_at"]) <= at(after)
    assert body["tracking"]["grants"][0]["tracking_id"] == record["tracking_id"]


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_tracking_grants_a_failed_investigation_too(
    config, alert_store, bot_client, github_client, tmp_path
):
    """Whatever its status, a delivered result gets a grant; the session
    decides whether to file it."""
    config.return_value = tracking_cfg(tmp_path)
    store = alert_store.return_value
    store.advance.return_value = True
    payload = copy.deepcopy(load_fixture("findings-payload-v4.json"))
    payload["results"][0]["status"] = "failed"
    payload["results"][0]["failure_reason"] = "the session ran out of turns"

    response = deliver_findings(payload, [autofix_row()])

    assert len(json.loads(response["body"])["tracking"]["grants"]) == 1


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_tracking_is_null_when_issue_filing_is_off(
    config, alert_store, bot_client, github_client, tmp_path
):
    from receiver.config import load_config
    from tests.test_config import VALID, write

    config.return_value = load_config(write(tmp_path, VALID))
    store = alert_store.return_value
    store.advance.return_value = True

    response = deliver_findings(load_fixture("findings-payload-v4.json"), [autofix_row()])

    assert json.loads(response["body"]) == {"autofix": None, "tracking": None}
    store.put_tracking_grant.assert_not_called()


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_tracking_is_null_for_an_environment_not_listed(
    config, alert_store, bot_client, github_client, tmp_path
):
    config.return_value = tracking_cfg(tmp_path, environments="[prod]")
    store = alert_store.return_value
    store.advance.return_value = True

    response = deliver_findings(load_fixture("findings-payload-v4.json"), [autofix_row()])

    assert json.loads(response["body"])["tracking"] is None
    store.put_tracking_grant.assert_not_called()


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_tracking_covers_every_environment_when_none_is_listed(
    config, alert_store, bot_client, github_client, tmp_path
):
    config.return_value = tracking_cfg(tmp_path, environments="[]")
    store = alert_store.return_value
    store.advance.return_value = True

    response = deliver_findings(load_fixture("findings-payload-v4.json"), [autofix_row()])

    assert len(json.loads(response["body"])["tracking"]["grants"]) == 1


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_tracking_grant_crash_never_costs_the_findings_reply(
    config, alert_store, bot_client, github_client, tmp_path, caplog
):
    config.return_value = tracking_cfg(tmp_path)
    store = alert_store.return_value
    store.advance.return_value = True
    store.put_tracking_grant.side_effect = RuntimeError("dynamo down")

    with caplog.at_level(logging.ERROR):
        response = deliver_findings(load_fixture("findings-payload-v4.json"), [autofix_row()])

    assert response["statusCode"] == 200
    bot_client.return_value.reply_card_in_thread.assert_called_once()
    assert json.loads(response["body"]) == {"autofix": None, "tracking": None}
    assert f"{observability.ISSUE_TRACKING_FAILED_MARKER} CHECKOUT-4B2" in caplog.text


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_tracking_is_null_when_no_row_wins_the_delivery_claim(
    config, alert_store, bot_client, github_client, tmp_path
):
    """Every row already delivered, or lost to the deadline sweep: nothing
    was delivered, so nothing is granted, and the block is null rather than
    an object with an empty `grants` list."""
    config.return_value = tracking_cfg(tmp_path)
    store = alert_store.return_value
    store.advance.return_value = False

    response = deliver_findings(two_passing_results(), two_autofix_rows())

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"autofix": None, "tracking": None}
    store.put_tracking_grant.assert_not_called()
    bot_client.return_value.reply_card_in_thread.assert_not_called()


from receiver.handler import route


def callback_event(token="cb-token", **body):
    payload = {"dispatch_id": "d-1", "status": "aborted_drift", **body}
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    return {
        "rawPath": "/autofix-result",
        "requestContext": {"http": {"method": "POST", "path": "/autofix-result"}},
        "headers": headers,
        "body": json.dumps(payload),
    }


def dispatch_record(token="cb-token"):
    from receiver.store import AlertStore

    return {
        "dispatch_id": "d-1",
        "short_id": "CHECKOUT-4B2",
        "conversation_id": "conv-1",
        "message_id": "msg-9",
        "callback_token_hash": AlertStore.hash_token(token),
        "status": "dispatched",
    }


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_bad_callback_token_is_unauthorized(alert_store, bot_client):
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record("other-token")

    response = route(callback_event())

    assert response["statusCode"] == 401
    bot_client.return_value.reply_in_thread.assert_not_called()


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_wrong_token_is_checked_with_a_constant_time_comparison(alert_store, bot_client):
    """Pins the use of hmac.compare_digest specifically: reverting to a plain
    `!=` leaves the rest of this suite green, since the status code doesn't
    change either way."""
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record("other-token")

    with patch(
        "receiver.handler.hmac.compare_digest", wraps=hmac.compare_digest
    ) as compare:
        response = route(callback_event())

    assert response["statusCode"] == 401
    compare.assert_called_once()


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_an_unknown_dispatch_is_unauthorized(alert_store, bot_client):
    alert_store.return_value.get_autofix_dispatch.return_value = None

    assert route(callback_event())["statusCode"] == 401


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_an_unknown_status_is_a_400(alert_store, bot_client):
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()

    assert route(callback_event(status="merged"))["statusCode"] == 400


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_settled_dispatch_answers_200_without_reposting(alert_store, bot_client):
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = False

    assert route(callback_event())["statusCode"] == 200
    bot_client.return_value.reply_in_thread.assert_not_called()


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_failed_callback_logs_the_autofix_failed_marker(alert_store, bot_client, caplog):
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    route(callback_event(status="failed", run_url="https://run"))

    assert "AUTOFIX_FAILED" in caplog.text
    # `run_url` stays a read-and-log field, so a caller that ever does send
    # one still leaves it in the operator's logs.
    assert "https://run" in caplog.text
    # It does not reach the thread. The session's callback body has no run
    # URL to give, so the reply says what happened without promising a link.
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert "https://run" not in reply
    assert "(link unavailable)" not in reply


def test_autofix_result_without_a_token_is_401():
    assert route(callback_event(token=None))["statusCode"] == 401


def test_autofix_result_with_a_non_ascii_token_is_rejected_not_crashed():
    assert route(callback_event(token="tökén"))["statusCode"] == 401


FIX_SHA = "79bad4b79fb044dc6386fa690aae2bc3a6ebcc29"
FIX_TITLE = "Autofix CHECKOUT-4B2: guard the empty-cart total"
FIX_BODY = "Root cause.\n\nWhat changed.\n\npytest tests/test_cart.py: passed."


def fix_ready_event(**overrides):
    payload = {
        "base_sha": FIX_SHA,
        "files": [{"path": "src/cart.py", "content": "total = 0\n"}],
        "title": FIX_TITLE,
        "body": FIX_BODY,
    }
    payload.update(overrides)
    return callback_event(status="fix_ready", **payload)


def autofix_config(tmp_path):
    from receiver.config import load_config
    from tests.test_config import AUTOFIX, VALID, write

    return load_config(write(tmp_path, VALID + AUTOFIX))


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_fix_ready_callback_claims_then_opens_then_replies_with_the_link(
    config, alert_store, bot_client, github_client, tmp_path
):
    cfg = autofix_config(tmp_path)
    config.return_value = cfg
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True
    url = f"https://github.com/{cfg.target_repo}/pull/42"

    def open_after_claim(**kwargs):
        # The claim must precede the first GitHub call, or the sweep could
        # expire the record while the pull request is being built.
        assert store.advance_autofix.call_args.args == ("d-1", "dispatched", "opening")
        return PullRequestOutcome(url=url)

    github_client.return_value.open_fix_pr.side_effect = open_after_claim

    response = route(fix_ready_event())

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "pr_opened", "pr_url": url}
    claim, settle = store.advance_autofix.call_args_list
    assert claim.args == ("d-1", "dispatched", "opening")
    assert claim.kwargs["due_at"]
    assert settle.args == ("d-1", "opening", "pr_opened")
    assert settle.kwargs == {"extra": {"pr_url": url}}
    assert github_client.return_value.open_fix_pr.call_args.kwargs == {
        "repo": cfg.target_repo,
        "base_sha": FIX_SHA,
        "base_branch": cfg.autofix_base_branch,
        "branch": "autofix/checkout-4b2-d-1",
        "files": [("src/cart.py", "total = 0\n")],
        "changes": [],
        "title": FIX_TITLE,
        "body": f"{FIX_BODY}\n\nFixes CHECKOUT-4B2",
        "commit_message": f"{FIX_TITLE}\n\nFixes CHECKOUT-4B2",
    }
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert reply == f"Autofix PR opened: {url}"


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_malformed_fix_payload_is_a_400_that_settles_failed_without_github(
    config, alert_store, bot_client, github_client, tmp_path, caplog
):
    config.return_value = autofix_config(tmp_path)
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    with caplog.at_level(logging.ERROR):
        response = route(fix_ready_event(files=[]))

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {
        "status": "failed", "reason": "files must be a non-empty list",
    }
    assert store.advance_autofix.call_args_list == [
        call("d-1", "dispatched", "failed", extra={"failure": "files must be a non-empty list"})
    ]
    github_client.return_value.open_fix_pr.assert_not_called()
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert reply == "Autofix failed. No pull request was opened."
    assert observability.AUTOFIX_FAILED_MARKER in caplog.text


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_forbidden_or_excluded_path_is_rejected_before_any_github_call(
    config, alert_store, bot_client, github_client, tmp_path
):
    """The AUTOFIX config excludes `infra/**`; `.github/*` is forbidden for
    every deployment. Both must fail on the receiver, where an injected
    instruction cannot argue with them."""
    config.return_value = autofix_config(tmp_path)
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    for path in (".github/workflows/ci.yml", "infra/stack.py"):
        response = route(fix_ready_event(files=[{"path": path, "content": ""}]))

        assert response["statusCode"] == 400, path
        assert json.loads(response["body"])["reason"] == f"path {path} is excluded"

    github_client.return_value.open_fix_pr.assert_not_called()


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_lost_claim_opens_nothing_and_returns_the_stored_outcome(
    config, alert_store, bot_client, github_client, tmp_path
):
    """A replayed fix_ready, or one the sweep beat: the record is already
    settled, so the session reads that outcome and nothing is opened."""
    cfg = autofix_config(tmp_path)
    config.return_value = cfg
    store = alert_store.return_value
    url = f"https://github.com/{cfg.target_repo}/pull/42"
    store.get_autofix_dispatch.side_effect = [
        dispatch_record(),
        {**dispatch_record(), "status": "pr_opened", "pr_url": url},
    ]
    store.advance_autofix.return_value = False

    response = route(fix_ready_event())

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "pr_opened", "pr_url": url}
    github_client.return_value.open_fix_pr.assert_not_called()
    bot_client.return_value.reply_in_thread.assert_not_called()


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_pull_request_that_did_not_open_settles_failed_and_says_so(
    config, alert_store, bot_client, github_client, tmp_path, caplog
):
    config.return_value = autofix_config(tmp_path)
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True
    github_client.return_value.open_fix_pr.return_value = PullRequestOutcome()

    with caplog.at_level(logging.ERROR):
        response = route(fix_ready_event())

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "status": "failed", "reason": "pull request not opened",
    }
    assert store.advance_autofix.call_args == call(
        "d-1", "opening", "failed", extra={"failure": "pull request not opened"}
    )
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert reply == "Autofix failed. No pull request was opened."
    assert observability.AUTOFIX_FAILED_MARKER in caplog.text


PATCH = (
    b"diff --git a/src/cart.py b/src/cart.py\n"
    b"index 5f1d6a1..2e3a1b0 100644\n"
    b"--- a/src/cart.py\n"
    b"+++ b/src/cart.py\n"
    b"@@ -1 +1 @@\n"
    b"-total = None\n"
    b"+total = 0\n"
)


def patch_ready_event(**overrides):
    fields = {"files": None, "patch": base64.b64encode(PATCH).decode()}
    fields.update(overrides)
    return fix_ready_event(**fields)


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_patch_reaches_github_as_parsed_changes(
    config, alert_store, bot_client, github_client, tmp_path
):
    cfg = autofix_config(tmp_path)
    config.return_value = cfg
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True
    url = f"https://github.com/{cfg.target_repo}/pull/42"
    github_client.return_value.open_fix_pr.return_value = PullRequestOutcome(url=url)

    response = route(patch_ready_event())

    assert json.loads(response["body"]) == {"status": "pr_opened", "pr_url": url}
    kwargs = github_client.return_value.open_fix_pr.call_args.kwargs
    assert kwargs["files"] == []
    assert [(c.old_path, c.new_path) for c in kwargs["changes"]] == [
        ("src/cart.py", "src/cart.py")
    ]
    assert kwargs["body"] == f"{FIX_BODY}\n\nFixes CHECKOUT-4B2"


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_patch_that_does_not_apply_settles_failed_with_its_reason(
    config, alert_store, bot_client, github_client, tmp_path, caplog
):
    """The reason is the one thing the session can act on, and the one thing
    an operator reading the record needs: which file, and why."""
    config.return_value = autofix_config(tmp_path)
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True
    reason = "hunk 1 for src/cart.py does not match base_sha at line 1"
    github_client.return_value.open_fix_pr.return_value = PullRequestOutcome(failure=reason)

    with caplog.at_level(logging.ERROR):
        response = route(patch_ready_event())

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "failed", "reason": reason}
    assert store.advance_autofix.call_args == call(
        "d-1", "opening", "failed", extra={"failure": reason}
    )
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert reply == "Autofix failed. No pull request was opened."
    assert reason in caplog.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"files": [{"path": "src/cart.py", "content": ""}]},
        {"patch": None},
    ],
    ids=["both", "neither"],
)
@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_fix_must_carry_exactly_one_form(
    config, alert_store, bot_client, github_client, tmp_path, overrides
):
    config.return_value = autofix_config(tmp_path)
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    response = route(patch_ready_event(**overrides))

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["reason"] == "send exactly one of files or patch"
    github_client.return_value.open_fix_pr.assert_not_called()


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_crash_inside_the_fix_branch_settles_failed_instead_of_a_5xx(
    config, alert_store, bot_client, github_client, tmp_path, caplog
):
    """A record left at `opening` by an unhandled exception would wait for
    the sweep with a thread that reads as still in progress; the branch
    must settle it itself."""
    config.return_value = autofix_config(tmp_path)
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True
    github_client.return_value.open_fix_pr.side_effect = RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        response = route(fix_ready_event())

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "failed", "reason": "receiver crashed"}
    assert store.advance_autofix.call_args == call(
        "d-1", "opening", "failed", extra={"failure": "receiver crashed"}
    )
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert reply == "Autofix failed. No pull request was opened."
    assert observability.AUTOFIX_FAILED_MARKER in caplog.text


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_session_claiming_pr_opened_gets_a_400(alert_store, bot_client):
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()

    response = route(callback_event(status="pr_opened", pr_url="https://pr"))

    assert response["statusCode"] == 400
    store.advance_autofix.assert_not_called()
    bot_client.return_value.reply_in_thread.assert_not_called()


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_the_pull_request_url_is_encoded_for_the_thread_and_raw_for_the_session(
    config, alert_store, bot_client, github_client, tmp_path
):
    """`_` is common in real repository names and is markdown on the Teams
    surface, so the reply percent-encodes it; the session and the record
    get GitHub's URL untouched."""
    config.return_value = autofix_config(tmp_path)
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True
    url = "https://github.com/acme-tools/my_repo/pull/7"
    github_client.return_value.open_fix_pr.return_value = PullRequestOutcome(url=url)

    response = route(fix_ready_event())

    assert json.loads(response["body"])["pr_url"] == url
    assert store.advance_autofix.call_args.kwargs == {"extra": {"pr_url": url}}
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert reply == "Autofix PR opened: https://github.com/acme-tools/my%5Frepo/pull/7"


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_terminal_status_advances_from_dispatched_and_replies(alert_store, bot_client):
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    response = route(callback_event(status="aborted_drift"))

    assert response["statusCode"] == 200
    assert store.advance_autofix.call_args == call("d-1", "dispatched", "aborted_drift")
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert reply.startswith("Autofix skipped: the base branch has moved")


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_session_reported_failure_is_recorded_as_one(alert_store, bot_client):
    """Every receiver-side failure writes its own reason, so the tracking
    backstop needs a positive value to tell the session's own `failed`
    apart from them, not an absent attribute."""
    from receiver.autofix import SESSION_REPORTED_FAILURE

    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    response = route(callback_event(status="failed"))

    assert response["statusCode"] == 200
    assert SESSION_REPORTED_FAILURE == "session reported"
    assert store.advance_autofix.call_args == call(
        "d-1", "dispatched", "failed", extra={"failure": "session reported"}
    )


@pytest.mark.parametrize("status", ["aborted_drift", "not_reproducible", "declined_in_session"])
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_session_reported_stop_short_of_failure_records_no_failure(alert_store, bot_client, status):
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    route(callback_event(status=status))

    assert store.advance_autofix.call_args == call("d-1", "dispatched", status)


def test_a_replayed_session_reported_failure_still_reads_its_reason():
    """`settled_outcome` is the one existing reader of `failure`."""
    with patch.object(handler, "alert_store") as alert_store:
        alert_store.return_value.get_autofix_dispatch.return_value = {
            "status": "failed", "failure": "session reported",
        }
        assert handler.settled_outcome("d-1") == {"status": "failed", "reason": "session reported"}


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_settle_that_raises_after_the_pull_request_exists_retries_and_reports_it(
    config, alert_store, bot_client, github_client, tmp_path, caplog
):
    """The pull request is real once GitHub returns a URL. A settle that
    raises on the first try (a throttled or unavailable store) must not
    cost the record a false `failed`; the retry that follows tells the
    truth once it lands."""
    config.return_value = autofix_config(tmp_path)
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    url = "https://github.com/acme-tools/checkout/pull/42"
    github_client.return_value.open_fix_pr.return_value = PullRequestOutcome(url=url)
    store.advance_autofix.side_effect = [True, RuntimeError("throttled"), True]

    with caplog.at_level(logging.ERROR):
        response = route(fix_ready_event())

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "pr_opened", "pr_url": url}
    assert store.advance_autofix.call_args_list[1:] == [
        call("d-1", "opening", "pr_opened", extra={"pr_url": url})
    ] * 2
    assert bot_client.return_value.reply_in_thread.call_count == 1
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert reply == f"Autofix PR opened: {url}"
    assert observability.AUTOFIX_FAILED_MARKER in caplog.text


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_settle_that_keeps_raising_still_tells_the_truth_about_the_pull_request(
    config, alert_store, bot_client, github_client, tmp_path, caplog
):
    """Both settle attempts fail. The record is left at `opening` for the
    sweep, but the thread still gets the real link rather than the
    sweep's later opening-timeout reply being the only thing the reader
    sees, and nothing here ever calls the record `failed`."""
    config.return_value = autofix_config(tmp_path)
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    url = "https://github.com/acme-tools/checkout/pull/42"
    github_client.return_value.open_fix_pr.return_value = PullRequestOutcome(url=url)
    store.advance_autofix.side_effect = [True, RuntimeError("down"), RuntimeError("down")]

    with caplog.at_level(logging.ERROR):
        response = route(fix_ready_event())

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "pr_opened", "pr_url": url}
    assert bot_client.return_value.reply_in_thread.call_count == 1
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert reply == f"Autofix PR opened: {url}"
    assert url in caplog.text
    assert observability.AUTOFIX_FAILED_MARKER in caplog.text
    assert not any(c.args[2] == "failed" for c in store.advance_autofix.call_args_list)


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_chat_transport_failure_after_the_pull_request_exists_never_records_failed(
    config, alert_store, bot_client, github_client, tmp_path, caplog
):
    """Both settle attempts fail, and the chat client's own attempt to post
    the pull request link fails too, with a transport error `post_completion`
    does not wrap. That must still never let the record land at `failed`:
    the pull request exists, and the crash guard is not allowed to see it."""
    config.return_value = autofix_config(tmp_path)
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    url = "https://github.com/acme-tools/checkout/pull/42"
    github_client.return_value.open_fix_pr.return_value = PullRequestOutcome(url=url)
    store.advance_autofix.side_effect = [True, RuntimeError("down"), RuntimeError("down")]
    bot_client.return_value.reply_in_thread.side_effect = ConnectionError("chat down")

    with caplog.at_level(logging.ERROR):
        response = route(fix_ready_event())

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "pr_opened", "pr_url": url}
    assert not any(c.args[2] == "failed" for c in store.advance_autofix.call_args_list)
    assert observability.DELIVERY_FAILURE_MARKER in caplog.text


# ---- Issue tracking: the /issues route, up to the claims ----

from types import SimpleNamespace

from receiver.github_app import FieldsOutcome, IssueCall, MintedToken

TRACKING_TOKEN = "trk-token"
TARGET_API = "https://api.github.com/repos/acme-tools/checkout"
FILE_ISSUE = {
    "op": "file_issue",
    "title": "Fix: empty cart total is None",
    "body": "Problem.",
    "type": "Bug",
    "labels": ["backend"],
    "search_unavailable": False,
}
DUPLICATE = {"op": "comment", "issue_number": 42, "relation": "duplicate", "body": "Seen again."}
REOPEN = {"op": "reopen", "issue_number": 42, "body": "It came back."}


def issues_event(token=TRACKING_TOKEN, tracking_id="t-1", **body):
    payload = {"tracking_id": tracking_id, **body}
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    return {
        "rawPath": "/issues",
        "requestContext": {"http": {"method": "POST", "path": "/issues"}},
        "headers": headers,
        "body": json.dumps(payload),
    }


def grant_row(**overrides):
    from receiver.store import AlertStore

    row = {
        "tracking_id": "t-1",
        "issue_id": "1000000007",
        "short_id": "CHECKOUT-4B2",
        "environment": "staging",
        "release": "79bad4b79fb044dc6386fa690aae2bc3a6ebcc29",
        "conversation_id": "conv-1",
        "message_id": "msg-9",
        "token_hash": AlertStore.hash_token(TRACKING_TOKEN),
        "expect": "always",
        "expires_at": "2999-01-01T00:00:00Z",
        "status": "open",
    }
    row.update(overrides)
    return row


def an_issue(state="open", **overrides):
    issue = {
        "number": 42,
        "state": state,
        "repository_url": TARGET_API,
        "html_url": "https://github.com/acme-tools/checkout/issues/42",
        "node_id": "I_42",
    }
    issue.update(overrides)
    return issue


@pytest.fixture
def proxy(tmp_path):
    """Every collaborator `/issues` reaches, patched, with issue filing on
    for staging and every claim succeeding."""
    cfg = tracking_cfg(tmp_path, autofix=True)
    store, github, bot = MagicMock(), MagicMock(), MagicMock()
    store.get_tracking_grant.return_value = grant_row()
    store.get_issue_op.return_value = None
    for claim in (
        store.claim_grant_cap, store.claim_daily_issue_cap, store.claim_issue_op,
        store.claim_tracking_issue, store.settle_issue_op, store.settle_tracked,
        store.release_tracking_issue,
    ):
        claim.return_value = True
    github.mint_token.return_value = MintedToken(token="ghs_issue", expires_at="2999-01-01T01:00:00Z")
    github.fetch_issue.return_value = IssueCall(ok=True, status=200, body=an_issue())
    github.list_label_names.return_value = ["Backend", "frontend"]
    with patch.object(handler, "config", return_value=cfg), \
         patch.object(handler, "alert_store", return_value=store), \
         patch.object(handler, "github_client", return_value=github), \
         patch.object(handler, "bot_client", return_value=bot):
        yield SimpleNamespace(cfg=cfg, store=store, github=github, bot=bot)


@pytest.fixture
def handed_off():
    """Stands in for the back half, so these tests stop at the hand-off."""
    with patch.object(
        handler, "run_issue_op", create=True,
        return_value=handler.respond_json(200, {"status": "done"}),
    ) as run:
        yield run


def status_of(response) -> tuple[int, dict | str]:
    body = response["body"]
    if response["headers"]["content-type"] == "application/json":
        body = json.loads(body)
    return response["statusCode"], body


def test_issue_route_is_not_served_while_issue_filing_is_off(proxy, handed_off, tmp_path):
    from receiver.config import load_config
    from tests.test_config import VALID, write

    with patch.object(handler, "config", return_value=load_config(write(tmp_path, VALID))):
        response = route(issues_event(**FILE_ISSUE))

    assert status_of(response) == (404, "not found")
    proxy.store.get_tracking_grant.assert_not_called()


def test_issue_route_answers_only_post():
    assert route({**issues_event(**FILE_ISSUE), "requestContext": {"http": {"method": "GET"}}})[
        "statusCode"
    ] == 404


@pytest.mark.parametrize("token", [None, "", "tökén"])
def test_issue_route_without_a_usable_token_is_401(proxy, handed_off, token):
    assert status_of(route(issues_event(token=token, **FILE_ISSUE))) == (401, "unauthorized")


def test_issue_route_with_an_unknown_grant_is_401_after_the_same_hash(proxy, handed_off):
    from receiver.store import AlertStore

    proxy.store.get_tracking_grant.return_value = None
    with patch.object(handler.AlertStore, "hash_token", wraps=AlertStore.hash_token) as hashed:
        response = route(issues_event(**FILE_ISSUE))

    assert status_of(response) == (401, "unauthorized")
    hashed.assert_called_once_with(TRACKING_TOKEN)


def test_issue_route_with_a_wrong_token_is_401_by_constant_time_compare(proxy, handed_off):
    with patch("receiver.handler.hmac.compare_digest", wraps=hmac.compare_digest) as compare:
        response = route(issues_event(token="other-token", **FILE_ISSUE))

    assert status_of(response) == (401, "unauthorized")
    compare.assert_called_once()
    handed_off.assert_not_called()


def test_issue_route_never_looks_up_an_oversized_tracking_id(proxy, handed_off):
    response = route(issues_event(tracking_id="t" * 65, **FILE_ISSUE))

    assert status_of(response) == (401, "unauthorized")
    proxy.store.get_tracking_grant.assert_not_called()


def test_issue_route_with_an_expired_grant_is_401_expired(proxy, handed_off):
    proxy.store.get_tracking_grant.return_value = grant_row(expires_at="2020-01-01T00:00:00Z")

    assert status_of(route(issues_event(**FILE_ISSUE))) == (401, "expired")
    proxy.github.mint_token.assert_not_called()


@pytest.mark.parametrize("raw", ["not json", "[1, 2]"])
def test_issue_route_with_an_unusable_body_is_400(proxy, handed_off, raw):
    event = {**issues_event(**FILE_ISSUE), "body": raw}

    assert status_of(route(event)) == (400, "unusable body")


def test_issue_route_refuses_a_grant_whose_environment_was_delisted(proxy, handed_off):
    proxy.store.get_tracking_grant.return_value = grant_row(environment="prod")

    assert status_of(route(issues_event(**FILE_ISSUE))) == (
        409, {"status": "refused", "reason": "environment not enabled"},
    )
    proxy.github.mint_token.assert_not_called()


def test_issue_route_rejects_an_invalid_op_before_any_read_or_claim(proxy, handed_off, caplog):
    with caplog.at_level(logging.ERROR):
        response = route(issues_event(op="close_issue", issue_number=42))

    code, body = status_of(response)
    assert code == 400
    assert body["status"] == "rejected"
    assert body["reason"].startswith("op must be one of")
    assert f"{observability.ISSUE_TRACKING_FAILED_MARKER} CHECKOUT-4B2 issue op rejected" in caplog.text
    proxy.store.get_issue_op.assert_not_called()
    proxy.github.mint_token.assert_not_called()


def test_issue_route_replays_a_done_op_with_no_github_call_or_reply(proxy, handed_off):
    proxy.store.get_issue_op.return_value = {
        "state": "done",
        "result": json.dumps(
            {"op": "file_issue", "issue_number": 9, "url": "https://x/9", "warnings": []}
        ),
    }

    code, body = status_of(route(issues_event(**FILE_ISSUE)))

    assert code == 200
    assert body == {
        "op": "file_issue", "issue_number": 9, "url": "https://x/9", "warnings": [],
        "status": "done", "replayed": True, "thread_notified": False,
    }
    proxy.github.mint_token.assert_not_called()
    proxy.store.claim_grant_cap.assert_not_called()
    proxy.bot.reply_in_thread.assert_not_called()
    handed_off.assert_not_called()


def test_issue_route_finds_the_replay_record_by_the_ops_canonical_key(proxy, handed_off):
    from receiver import issues

    route(issues_event(**FILE_ISSUE))

    expected = issues.op_key({"tracking_id": "t-1", **FILE_ISSUE})
    assert proxy.store.get_issue_op.call_args.args == ("t-1", expected)


def test_issue_route_refuses_an_op_still_sending(proxy, handed_off):
    proxy.store.get_issue_op.return_value = {"state": "sending", "attempts": 1}

    assert status_of(route(issues_event(**FILE_ISSUE))) == (
        409, {"status": "refused", "reason": "in progress"},
    )
    proxy.github.mint_token.assert_not_called()


def test_issue_route_retries_a_failed_op_once(proxy, handed_off):
    proxy.store.get_issue_op.return_value = {"state": "failed", "attempts": 1}

    assert route(issues_event(**FILE_ISSUE))["statusCode"] == 200
    handed_off.assert_called_once()
    assert proxy.store.claim_issue_op.call_args.kwargs == {"op": "file_issue", "max_attempts": 2}


def test_issue_route_refuses_a_failed_op_past_its_retry(proxy, handed_off):
    proxy.store.get_issue_op.return_value = {"state": "failed", "attempts": 2}

    assert status_of(route(issues_event(**FILE_ISSUE))) == (
        409, {"status": "refused", "reason": "retry limit reached"},
    )
    handed_off.assert_not_called()


@pytest.mark.parametrize("body", [FILE_ISSUE, DUPLICATE, REOPEN])
def test_issue_route_allows_one_tracking_outcome_per_alert(proxy, handed_off, body):
    proxy.store.get_tracking_grant.return_value = grant_row(status="tracked", tracked_issue=7)

    assert status_of(route(issues_event(**body))) == (
        409, {"status": "refused", "reason": "already tracked in #7"},
    )
    proxy.github.mint_token.assert_not_called()


def test_issue_route_takes_a_related_comment_after_the_alert_is_tracked(proxy, handed_off):
    proxy.store.get_tracking_grant.return_value = grant_row(status="tracked", tracked_issue=7)

    assert route(issues_event(**{**DUPLICATE, "relation": "related"}))["statusCode"] == 200
    handed_off.assert_called_once()


@pytest.mark.parametrize(
    "body",
    [
        {"op": "set_fields", "issue_number": 42, "fields": {"Priority": "High"}},
        {"op": "add_to_project", "issue_number": 42},
    ],
)
def test_issue_route_touches_fields_and_board_only_on_the_alerts_own_issue(proxy, handed_off, body):
    proxy.store.get_tracking_grant.return_value = grant_row(
        status="tracked", tracked_issue=9, created_issue=9
    )

    assert status_of(route(issues_event(**body))) == (
        409, {"status": "refused", "reason": "only the issue this alert filed"},
    )
    proxy.github.mint_token.assert_not_called()

    proxy.store.get_tracking_grant.return_value = grant_row(
        status="tracked", tracked_issue=42, created_issue=42
    )
    assert route(issues_event(**body))["statusCode"] == 200


@pytest.mark.parametrize(
    ("op", "permissions"),
    [
        (FILE_ISSUE, {"issues": "write"}),
        (REOPEN, {"issues": "write"}),
        ({"op": "add_to_project", "issue_number": 42}, {"issues": "read", "organization_projects": "write"}),
    ],
)
def test_issue_route_mints_the_ops_own_permissions_for_the_target_repo(
    proxy, handed_off, op, permissions
):
    proxy.store.get_tracking_grant.return_value = grant_row(created_issue=42)
    proxy.github.fetch_issue.return_value = IssueCall(ok=True, status=200, body=an_issue("closed"))

    route(issues_event(**op))

    call = proxy.github.mint_token.call_args
    assert call.args == ("acme-tools/checkout", permissions)
    assert call.kwargs["deadline"] > 0


def test_issue_route_answers_502_when_the_mint_fails_and_claims_nothing(proxy, handed_off, caplog):
    proxy.github.mint_token.return_value = None

    with caplog.at_level(logging.ERROR):
        assert status_of(route(issues_event(**FILE_ISSUE))) == (
            502, {"status": "failed", "reason": "token mint failed"},
        )
    assert observability.ISSUE_TRACKING_FAILED_MARKER in caplog.text
    proxy.store.claim_grant_cap.assert_not_called()


def test_issue_route_reads_the_issue_in_the_target_repo_before_a_comment(proxy, handed_off):
    route(issues_event(**DUPLICATE))

    call = proxy.github.fetch_issue.call_args
    assert call.args == ("acme-tools/checkout", 42)
    assert call.kwargs["token"] == "ghs_issue"
    assert handed_off.call_args.kwargs["issue"] == an_issue()


@pytest.mark.parametrize(
    ("read", "reason"),
    [
        (IssueCall(ok=False, status=301, step="read the issue"), "the issue is not in the target repository"),
        (IssueCall(ok=False, status=404, step="read the issue"), "the issue is not in the target repository"),
        (
            IssueCall(ok=True, status=200, body=an_issue(repository_url="https://api.github.com/repos/acme-tools/other")),
            "the issue is not in the target repository",
        ),
        (
            IssueCall(ok=True, status=200, body=an_issue(pull_request={"url": "x"})),
            "the number names a pull request",
        ),
    ],
)
def test_issue_route_refuses_a_number_outside_the_target_repo_with_no_write(
    proxy, handed_off, read, reason
):
    proxy.github.fetch_issue.return_value = read

    assert status_of(route(issues_event(**DUPLICATE))) == (409, {"status": "refused", "reason": reason})
    proxy.store.claim_grant_cap.assert_not_called()
    handed_off.assert_not_called()


def test_issue_route_matches_the_target_repo_ignoring_case(proxy, handed_off):
    proxy.github.fetch_issue.return_value = IssueCall(
        ok=True, status=200, body=an_issue(repository_url=TARGET_API.upper().replace("HTTPS", "https"))
    )

    assert route(issues_event(**DUPLICATE))["statusCode"] == 200


def test_issue_route_answers_502_when_github_fails_the_read(proxy, handed_off):
    proxy.github.fetch_issue.return_value = IssueCall(ok=False, status=500, step="read the issue")

    assert status_of(route(issues_event(**DUPLICATE))) == (
        502, {"status": "failed", "reason": "read the issue"},
    )
    proxy.store.claim_grant_cap.assert_not_called()


def test_issue_route_refuses_to_reopen_an_open_issue(proxy, handed_off):
    assert status_of(route(issues_event(**REOPEN))) == (
        409, {"status": "refused", "reason": "the issue is not closed"},
    )


def test_issue_route_takes_a_duplicate_comment_on_a_closed_issue(proxy, handed_off):
    proxy.github.fetch_issue.return_value = IssueCall(ok=True, status=200, body=an_issue("closed"))

    assert route(issues_event(**DUPLICATE))["statusCode"] == 200
    assert handed_off.call_args.kwargs["issue"]["state"] == "closed"


def test_issue_route_lists_labels_before_a_create_and_hands_them_on(proxy, handed_off):
    route(issues_event(**FILE_ISSUE))

    assert proxy.github.list_label_names.call_args.args == ("acme-tools/checkout",)
    assert handed_off.call_args.kwargs["repo_labels"] == ["Backend", "frontend"]
    proxy.github.fetch_issue.assert_not_called()


def test_issue_route_skips_the_label_list_for_a_create_with_no_labels(proxy, handed_off):
    route(issues_event(**{k: v for k, v in FILE_ISSUE.items() if k != "labels"}))

    proxy.github.list_label_names.assert_not_called()
    assert handed_off.call_args.kwargs["repo_labels"] == []


def test_issue_route_answers_502_when_the_label_list_fails(proxy, handed_off):
    proxy.github.list_label_names.return_value = None

    assert status_of(route(issues_event(**FILE_ISSUE))) == (
        502, {"status": "failed", "reason": "list the labels"},
    )
    proxy.store.claim_grant_cap.assert_not_called()


def test_issue_route_claims_cheapest_first_and_the_days_budget_last(proxy, handed_off):
    route(issues_event(**FILE_ISSUE))

    claims = [
        c for c in proxy.store.mock_calls
        if c[0] in ("claim_grant_cap", "claim_daily_issue_cap", "claim_issue_op")
    ]
    assert [(c[0], c.args[:2] if c[0] != "claim_daily_issue_cap" else c.args[0]) for c in claims] == [
        ("claim_grant_cap", ("t-1", "file_issue")),
        ("claim_daily_issue_cap", "issue-creates"),
        ("claim_daily_issue_cap", "issue-writes"),
        ("claim_issue_op", ("t-1", handler.issues.op_key({"tracking_id": "t-1", **FILE_ISSUE}))),
    ]
    assert claims[0].args[2] == 2
    assert claims[1].args[2] == 25
    assert claims[2].args[2] == 100


def test_issue_route_spends_no_create_budget_on_a_comment(proxy, handed_off):
    route(issues_event(**DUPLICATE))

    counters = [c.args[0] for c in proxy.store.claim_daily_issue_cap.call_args_list]
    assert counters == ["issue-writes"]


@pytest.mark.parametrize(
    ("refused_claim", "reason", "later"),
    [
        ("claim_grant_cap", "file_issue cap reached for this alert", "claim_daily_issue_cap"),
        ("claim_issue_op", "in progress", None),
    ],
)
def test_issue_route_refuses_at_a_spent_claim_and_spends_nothing_after_it(
    proxy, handed_off, refused_claim, reason, later
):
    getattr(proxy.store, refused_claim).return_value = False

    assert status_of(route(issues_event(**FILE_ISSUE))) == (409, {"status": "refused", "reason": reason})
    if later:
        getattr(proxy.store, later).assert_not_called()
    handed_off.assert_not_called()


@pytest.mark.parametrize(
    ("spent", "reason", "writes_claimed"),
    [
        ("issue-creates", "daily issue create cap reached", False),
        ("issue-writes", "daily issue write cap reached", True),
    ],
)
def test_issue_route_refuses_past_a_daily_cap(proxy, handed_off, spent, reason, writes_claimed):
    proxy.store.claim_daily_issue_cap.side_effect = lambda counter, day, cap: counter != spent

    assert status_of(route(issues_event(**FILE_ISSUE))) == (409, {"status": "refused", "reason": reason})
    counters = [c.args[0] for c in proxy.store.claim_daily_issue_cap.call_args_list]
    assert ("issue-writes" in counters) is writes_claimed
    proxy.store.claim_issue_op.assert_not_called()


def test_issue_route_hands_the_claimed_op_to_the_back_half(proxy, handed_off):
    response = route(issues_event(**DUPLICATE))

    assert response["statusCode"] == 200
    args = handed_off.call_args.args
    assert args[0] is proxy.cfg
    assert args[1] == grant_row()
    assert args[2] == {"tracking_id": "t-1", **DUPLICATE}
    assert args[3] == handler.issues.op_key({"tracking_id": "t-1", **DUPLICATE})
    assert args[4] == "ghs_issue"


# ---- Issue tracking: the /issues route, performing the op ----

CREATED = {
    "number": 9,
    "html_url": "https://github.com/acme-tools/checkout/issues/9",
    "node_id": "I_9",
    "labels": [{"name": "Backend"}],
    "type": {"name": "Bug"},
}


def done_body(response) -> dict:
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def test_issue_op_files_a_new_issue_and_links_it_in_the_thread(proxy):
    proxy.github.file_issue.return_value = IssueCall(ok=True, status=201, body=CREATED)

    body = done_body(route(issues_event(**FILE_ISSUE)))

    assert body == {
        "status": "done", "op": "file_issue", "issue_number": 9,
        "url": CREATED["html_url"], "warnings": [], "replayed": False, "thread_notified": True,
    }
    call = proxy.github.file_issue.call_args
    assert call.args == ("acme-tools/checkout",)
    assert call.kwargs["token"] == "ghs_issue"
    assert call.kwargs["title"] == "Fix: empty cart total is None"
    assert call.kwargs["body"] == (
        "Problem.\n\nSentinel · Sentry CHECKOUT-4B2 · staging · release 79bad4b"
    )
    # The repository's own spelling of the label goes out, not the session's.
    assert call.kwargs["labels"] == ["Backend"]
    assert call.kwargs["issue_type"] == "Bug"
    key = handler.issues.op_key({"tracking_id": "t-1", **FILE_ISSUE})
    assert proxy.store.claim_tracking_issue.call_args == call("t-1", key, search_unavailable=False)
    settled = proxy.store.settle_issue_op.call_args
    assert settled.args == ("t-1", key, "done")
    assert settled.kwargs["result"]["issue_number"] == 9
    assert proxy.store.settle_tracked.call_args == call(
        "t-1", key, issue_number=9, url=CREATED["html_url"], via="file_issue", node_id="I_9"
    )
    conversation_id, message_id, reply = proxy.bot.reply_in_thread.call_args.args
    assert (conversation_id, message_id) == ("conv-1", "msg-9")
    assert reply == "📌 Tracked in GitHub: https://github.com/acme-tools/checkout/issues/9"


def test_issue_op_settles_before_it_replies(proxy):
    proxy.github.file_issue.return_value = IssueCall(ok=True, status=201, body=CREATED)
    order = []
    proxy.store.settle_tracked.side_effect = lambda *a, **k: order.append("settle") or True
    proxy.bot.reply_in_thread.side_effect = lambda *a: order.append("reply")

    route(issues_event(**FILE_ISSUE))

    assert order == ["settle", "reply"]


def test_issue_op_redacts_the_title_and_body_it_sends(proxy, caplog):
    proxy.github.file_issue.return_value = IssueCall(ok=True, status=201, body=CREATED)

    with caplog.at_level(logging.INFO):
        route(issues_event(**{**FILE_ISSUE, "title": "Fix: fails for ops@example.com", "body": "From 10.0.0.1."}))

    call = proxy.github.file_issue.call_args
    assert call.kwargs["title"] == "Fix: fails for [redacted: email]"
    assert call.kwargs["body"].startswith("From [redacted: ip].")
    assert "REDACTIONS_APPLIED 2 for CHECKOUT-4B2" in caplog.text


def test_issue_op_puts_the_search_line_first_and_says_so_in_the_thread(proxy):
    proxy.github.file_issue.return_value = IssueCall(ok=True, status=201, body=CREATED)

    route(issues_event(**{**FILE_ISSUE, "search_unavailable": True}))

    sent = proxy.github.file_issue.call_args.kwargs["body"]
    assert sent.startswith(handler.issues.SEARCH_UNAVAILABLE_LINE + "\n\nProblem.")
    assert proxy.store.claim_tracking_issue.call_args.kwargs == {"search_unavailable": True}
    reply = proxy.bot.reply_in_thread.call_args.args[2]
    assert reply == (
        "📌 Tracked in GitHub: https://github.com/acme-tools/checkout/issues/9\n\n"
        "⚠️ The duplicate search was unavailable, so this issue may duplicate an existing one."
    )


def test_issue_op_drops_an_unknown_label_loudly(proxy, caplog):
    proxy.github.file_issue.return_value = IssueCall(ok=True, status=201, body=CREATED)

    with caplog.at_level(logging.ERROR):
        body = done_body(route(issues_event(**{**FILE_ISSUE, "labels": ["backend", "made-up"]})))

    assert proxy.github.file_issue.call_args.kwargs["labels"] == ["Backend"]
    assert body["warnings"] == ["label made-up is not defined in the repository and was not sent"]
    assert f"{observability.ISSUE_TRACKING_FAILED_MARKER} CHECKOUT-4B2 file_issue #9 landed with warnings" in caplog.text


def test_issue_op_reports_a_label_or_type_github_did_not_keep(proxy):
    proxy.github.file_issue.return_value = IssueCall(
        ok=True, status=201, body={**CREATED, "labels": [], "type": None}
    )

    body = done_body(route(issues_event(**FILE_ISSUE)))

    assert body["warnings"] == [
        "label Backend did not land on the issue",
        "type Bug did not land on the issue",
    ]
    # The issue still counts as created.
    assert proxy.store.settle_tracked.call_args.kwargs["via"] == "file_issue"


def test_issue_op_maps_githubs_422_on_create_to_a_rejection(proxy, caplog):
    proxy.github.file_issue.return_value = IssueCall(ok=False, status=422, step="create the issue")

    with caplog.at_level(logging.ERROR):
        response = route(issues_event(**FILE_ISSUE))

    assert status_of(response) == (
        400, {"status": "rejected", "reason": "GitHub refused the issue as sent"},
    )
    key = handler.issues.op_key({"tracking_id": "t-1", **FILE_ISSUE})
    assert proxy.store.settle_issue_op.call_args == call(
        "t-1", key, "failed", error="GitHub refused the issue as sent"
    )
    assert proxy.store.release_tracking_issue.call_args == call("t-1", key)
    assert observability.ISSUE_TRACKING_FAILED_MARKER in caplog.text
    proxy.bot.reply_in_thread.assert_not_called()


def test_issue_op_answers_502_naming_the_failed_step_and_frees_a_retry(proxy, caplog):
    proxy.github.file_issue.return_value = IssueCall(ok=False, status=503, step="create the issue")

    with caplog.at_level(logging.ERROR):
        response = route(issues_event(**FILE_ISSUE))

    assert status_of(response) == (502, {"status": "failed", "reason": "create the issue"})
    key = handler.issues.op_key({"tracking_id": "t-1", **FILE_ISSUE})
    assert proxy.store.settle_issue_op.call_args == call("t-1", key, "failed", error="create the issue")
    assert proxy.store.release_tracking_issue.call_args == call("t-1", key)
    proxy.store.settle_tracked.assert_not_called()
    assert f"{observability.ISSUE_TRACKING_FAILED_MARKER} CHECKOUT-4B2 file_issue failed" in caplog.text


def test_issue_op_refuses_a_second_tracking_op_that_lost_the_claim(proxy):
    proxy.store.claim_tracking_issue.return_value = False
    proxy.store.get_tracking_grant.side_effect = [grant_row(), grant_row(status="tracked", tracked_issue=5)]

    assert status_of(route(issues_event(**FILE_ISSUE))) == (
        409, {"status": "refused", "reason": "already tracked in #5"},
    )
    key = handler.issues.op_key({"tracking_id": "t-1", **FILE_ISSUE})
    assert proxy.store.settle_issue_op.call_args == call(
        "t-1", key, "failed", error="already tracked in #5"
    )
    proxy.github.file_issue.assert_not_called()


def test_issue_op_refuses_while_another_tracking_op_holds_the_grant(proxy):
    proxy.store.claim_tracking_issue.return_value = False

    assert status_of(route(issues_event(**FILE_ISSUE))) == (
        409, {"status": "refused", "reason": "another tracking operation is in progress"},
    )
    proxy.github.file_issue.assert_not_called()


@pytest.mark.parametrize(
    ("state", "reply"),
    [
        ("open", "📌 Already tracked in GitHub; this occurrence was added to {url}"),
        ("closed", "📌 Already tracked in GitHub by a closed issue; this occurrence was added to {url}"),
    ],
)
def test_issue_op_tracks_the_alert_with_a_duplicate_comment(proxy, state, reply):
    comment_url = "https://github.com/acme-tools/checkout/issues/42#issuecomment-1"
    proxy.github.fetch_issue.return_value = IssueCall(ok=True, status=200, body=an_issue(state))
    proxy.github.comment_on_issue.return_value = IssueCall(ok=True, status=201, body={"html_url": comment_url})

    body = done_body(route(issues_event(**DUPLICATE)))

    assert body["issue_number"] == 42
    assert body["url"] == comment_url
    call_args = proxy.github.comment_on_issue.call_args
    assert call_args.args == ("acme-tools/checkout", 42)
    assert call_args.kwargs["body"] == (
        "Seen again.\n\nSentinel · Sentry CHECKOUT-4B2 · staging · release 79bad4b"
    )
    assert proxy.store.settle_tracked.call_args.kwargs["via"] == "duplicate"
    assert proxy.bot.reply_in_thread.call_args.args[2] == reply.format(url=comment_url)


def test_issue_op_posts_nothing_for_a_related_comment(proxy):
    proxy.github.comment_on_issue.return_value = IssueCall(ok=True, status=201, body={"html_url": "c"})

    body = done_body(route(issues_event(**{**DUPLICATE, "relation": "related"})))

    assert body["thread_notified"] is False
    proxy.store.claim_tracking_issue.assert_not_called()
    proxy.store.settle_tracked.assert_not_called()
    proxy.bot.reply_in_thread.assert_not_called()


def test_issue_op_reopens_with_its_comment_and_says_so(proxy):
    proxy.github.fetch_issue.return_value = IssueCall(ok=True, status=200, body=an_issue("closed"))
    proxy.github.reopen_issue.return_value = IssueCall(
        ok=True, status=200, body=an_issue("open")
    )

    body = done_body(route(issues_event(**REOPEN)))

    assert body["url"] == "https://github.com/acme-tools/checkout/issues/42"
    assert proxy.github.reopen_issue.call_args.kwargs["body"].startswith("It came back.\n\nSentinel")
    assert proxy.store.settle_tracked.call_args.kwargs["via"] == "reopen"
    assert proxy.bot.reply_in_thread.call_args.args[2] == (
        "📌 Reopened in GitHub, since this error came back after the issue was closed: "
        "https://github.com/acme-tools/checkout/issues/42"
    )


def test_issue_op_names_the_reopen_step_when_the_reopen_fails_after_its_comment(proxy):
    proxy.github.fetch_issue.return_value = IssueCall(ok=True, status=200, body=an_issue("closed"))
    proxy.github.reopen_issue.return_value = IssueCall(ok=False, status=500, step="reopen the issue")

    assert status_of(route(issues_event(**REOPEN))) == (
        502, {"status": "failed", "reason": "reopen the issue"},
    )


def own_issue():
    return grant_row(
        status="tracked", tracked_issue=42, tracked_url=an_issue()["html_url"],
        created_issue=42, created_node_id="I_42",
    )


def test_issue_op_sets_fields_on_the_alerts_own_issue_quietly(proxy):
    proxy.store.get_tracking_grant.return_value = own_issue()
    proxy.github.set_fields.return_value = FieldsOutcome(ok=True, fields={"Priority": "High"})

    body = done_body(route(issues_event(op="set_fields", issue_number=42, fields={"Priority": "high"})))

    assert body["fields"] == {"Priority": "High"}
    assert body["url"] == an_issue()["html_url"]
    call_args = proxy.github.set_fields.call_args
    assert call_args.args == ("acme-tools/checkout",)
    assert call_args.kwargs["node_id"] == "I_42"
    assert call_args.kwargs["fields"] == {"Priority": "high"}
    proxy.bot.reply_in_thread.assert_not_called()
    proxy.store.settle_tracked.assert_not_called()


def test_issue_op_rejects_an_unknown_option_and_fails_a_value_that_did_not_persist(proxy):
    proxy.store.get_tracking_grant.return_value = own_issue()
    proxy.github.set_fields.return_value = FieldsOutcome(
        ok=False, rejected="unknown option for field Priority"
    )

    assert status_of(route(issues_event(op="set_fields", issue_number=42, fields={"Priority": "x"}))) == (
        400, {"status": "rejected", "reason": "unknown option for field Priority"},
    )

    proxy.github.set_fields.return_value = FieldsOutcome(ok=False, step="field values did not persist")
    assert status_of(route(issues_event(op="set_fields", issue_number=42, fields={"Priority": "y"}))) == (
        502, {"status": "failed", "reason": "field values did not persist"},
    )
    proxy.store.release_tracking_issue.assert_not_called()


def test_issue_op_adds_the_alerts_own_issue_to_the_board_quietly(proxy):
    proxy.store.get_tracking_grant.return_value = own_issue()
    proxy.github.add_to_project.return_value = IssueCall(ok=True, status=200, body={"item_id": "PVTI_1"})

    body = done_body(route(issues_event(op="add_to_project", issue_number=42)))

    assert body["issue_number"] == 42
    call_args = proxy.github.add_to_project.call_args
    assert call_args.args == ("acme-tools/checkout", 3)
    assert call_args.kwargs["node_id"] == "I_42"
    proxy.bot.reply_in_thread.assert_not_called()


def test_issue_op_still_answers_done_when_the_thread_reply_fails(proxy, caplog):
    proxy.github.file_issue.return_value = IssueCall(ok=True, status=201, body=CREATED)
    proxy.bot.reply_in_thread.side_effect = BotError("teams down")

    with caplog.at_level(logging.ERROR):
        body = done_body(route(issues_event(**FILE_ISSUE)))

    assert body["thread_notified"] is False
    assert f"{observability.DELIVERY_FAILURE_MARKER} issue tracking reply for CHECKOUT-4B2" in caplog.text
    proxy.store.release_tracking_issue.assert_not_called()


def test_issue_op_reports_an_issue_that_landed_after_the_grant_closed(proxy, caplog):
    proxy.github.file_issue.return_value = IssueCall(ok=True, status=201, body=CREATED)
    proxy.store.settle_tracked.return_value = False

    with caplog.at_level(logging.ERROR):
        body = done_body(route(issues_event(**FILE_ISSUE)))

    assert body["issue_number"] == 9
    assert "landed on #9 after the grant closed" in caplog.text
    proxy.bot.reply_in_thread.assert_called_once()


def test_issue_op_crash_settles_failed_and_releases_the_tracking_claim(proxy, caplog):
    proxy.github.file_issue.side_effect = RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        response = route(issues_event(**FILE_ISSUE))

    assert status_of(response) == (502, {"status": "failed", "reason": "receiver crashed"})
    key = handler.issues.op_key({"tracking_id": "t-1", **FILE_ISSUE})
    assert proxy.store.settle_issue_op.call_args == call("t-1", key, "failed", error="receiver crashed")
    assert proxy.store.release_tracking_issue.call_args == call("t-1", key)
    assert f"{observability.ISSUE_TRACKING_FAILED_MARKER} CHECKOUT-4B2 file_issue crashed" in caplog.text


def test_issue_op_crash_with_the_store_down_still_answers_502(proxy, caplog):
    proxy.github.file_issue.side_effect = RuntimeError("boom")
    proxy.store.settle_issue_op.side_effect = RuntimeError("dynamo down")

    with caplog.at_level(logging.ERROR):
        response = route(issues_event(**FILE_ISSUE))

    assert status_of(response) == (502, {"status": "failed", "reason": "receiver crashed"})
    assert "could not settle file_issue after the crash" in caplog.text


def test_issue_op_a_related_comment_crash_releases_nothing(proxy):
    proxy.github.comment_on_issue.side_effect = RuntimeError("boom")

    route(issues_event(**{**DUPLICATE, "relation": "related"}))

    proxy.store.release_tracking_issue.assert_not_called()


def test_issue_op_encodes_the_link_for_the_thread_and_not_for_the_session(proxy):
    raw = "https://github.com/acme-tools/my_repo/issues/9"
    proxy.github.file_issue.return_value = IssueCall(ok=True, status=201, body={**CREATED, "html_url": raw})

    body = done_body(route(issues_event(**FILE_ISSUE)))

    assert body["url"] == raw
    assert proxy.bot.reply_in_thread.call_args.args[2] == (
        "📌 Tracked in GitHub: https://github.com/acme-tools/my%5Frepo/issues/9"
    )


class FakeTrackingStore:
    """The tracking half of AlertStore, kept in memory, so a sequence of
    /issues calls sees each other's writes the way DynamoDB's conditional
    updates would."""

    def __init__(self, grant: dict):
        self.grant = dict(grant)
        self.ops: dict[str, dict] = {}
        self.days: dict[str, int] = {}

    def get_tracking_grant(self, tracking_id):
        return dict(self.grant) if tracking_id == self.grant["tracking_id"] else None

    def get_issue_op(self, tracking_id, key):
        record = self.ops.get(key)
        return dict(record) if record else None

    def claim_grant_cap(self, tracking_id, op, cap):
        spent = self.grant.get(f"n_{op}", 0)
        if spent >= cap:
            return False
        self.grant[f"n_{op}"] = spent + 1
        return True

    def claim_daily_issue_cap(self, counter, day, cap):
        if self.days.get(counter, 0) >= cap:
            return False
        self.days[counter] = self.days.get(counter, 0) + 1
        return True

    def claim_issue_op(self, tracking_id, key, *, op, max_attempts):
        record = self.ops.get(key)
        if record and not (record["state"] == "failed" and record["attempts"] < max_attempts):
            return False
        attempts = record["attempts"] + 1 if record else 1
        self.ops[key] = {"state": "sending", "op": op, "attempts": attempts}
        return True

    def settle_issue_op(self, tracking_id, key, state, *, result=None, error=""):
        record = self.ops.get(key)
        if not record or record["state"] != "sending":
            return False
        record["state"] = state
        if result is not None:
            record["result"] = json.dumps(result, sort_keys=True)
        if error:
            record["error"] = error
        return True

    def claim_tracking_issue(self, tracking_id, key, *, search_unavailable=False):
        if self.grant["status"] != "open" or self.grant.get("tracking_op") not in (None, key):
            return False
        self.grant["tracking_op"] = key
        if search_unavailable:
            self.grant["search_unavailable"] = True
        return True

    def release_tracking_issue(self, tracking_id, key):
        if self.grant.get("tracking_op") != key:
            return False
        del self.grant["tracking_op"]
        return True

    def settle_tracked(self, tracking_id, key, *, issue_number, url, via, node_id=""):
        if self.grant["status"] != "open" or self.grant.get("tracking_op") != key:
            return False
        self.grant.update(
            status="tracked", tracked_issue=issue_number, tracked_url=url, tracked_via=via
        )
        if via == "file_issue":
            self.grant.update(created_issue=issue_number, created_node_id=node_id)
        del self.grant["tracking_op"]
        return True


def test_issue_op_files_on_the_documented_retry_after_github_refuses_the_type(proxy):
    """GitHub refuses the type with a 422, which nothing was written for, so
    the answer is a 400 and the session retries once without `type` and
    `labels`. The retry files the issue and becomes the alert's tracking
    issue, and the thread hears exactly once. The two attempts are the
    grant's two file_issue attempts, and the rejected one still spent one
    create and one write from the day's budget: the claims run before the
    GitHub call, and a rejection refunds nothing."""
    store = FakeTrackingStore(grant_row())
    proxy.github.file_issue.side_effect = [
        IssueCall(ok=False, status=422, step="create the issue"),
        IssueCall(ok=True, status=201, body={**CREATED, "labels": [], "type": None}),
    ]
    retry = {key: value for key, value in FILE_ISSUE.items() if key not in ("type", "labels")}

    with patch.object(handler, "alert_store", return_value=store):
        first = route(issues_event(**FILE_ISSUE))
        second = route(issues_event(**retry))
        resent = route(issues_event(**retry))

    assert status_of(first) == (
        400, {"status": "rejected", "reason": "GitHub refused the issue as sent"},
    )
    body = done_body(second)
    assert body["issue_number"] == 9
    assert body["warnings"] == []
    assert body["thread_notified"] is True
    retried = proxy.github.file_issue.call_args.kwargs
    assert (retried["issue_type"], retried["labels"]) == ("", [])
    assert store.grant["status"] == "tracked"
    assert (store.grant["tracked_issue"], store.grant["tracked_via"]) == (9, "file_issue")
    assert store.grant["n_file_issue"] == 2
    assert store.days == {"issue-creates": 2, "issue-writes": 2}
    assert proxy.bot.reply_in_thread.call_count == 1
    # A resent retry is answered from its record: no third create, no reply.
    assert done_body(resent)["replayed"] is True
    assert proxy.github.file_issue.call_count == 2
    assert proxy.bot.reply_in_thread.call_count == 1


@pytest.mark.parametrize(
    "thread",
    [{"conversation_id": None}, {"message_id": None}, {"conversation_id": ""}],
    ids=["no-conversation", "no-message", "empty-conversation"],
)
def test_issue_op_with_no_thread_to_reply_in_still_answers_done(proxy, thread, caplog):
    """A grant whose alert card never posted has no thread. The op still
    settles and answers done; the reply is skipped with a warning, with no
    bot call and no delivery-failure line for an alarm to count."""
    grant = grant_row()
    for key, value in thread.items():
        if value is None:
            del grant[key]
        else:
            grant[key] = value
    proxy.store.get_tracking_grant.return_value = grant
    proxy.github.file_issue.return_value = IssueCall(ok=True, status=201, body=CREATED)

    with caplog.at_level(logging.WARNING):
        body = done_body(route(issues_event(**FILE_ISSUE)))

    assert body["thread_notified"] is False
    proxy.bot.reply_in_thread.assert_not_called()
    assert proxy.store.settle_tracked.call_args.kwargs["via"] == "file_issue"
    assert "no thread to reply in for CHECKOUT-4B2" in caplog.text
    assert observability.DELIVERY_FAILURE_MARKER not in caplog.text


def test_issue_op_crash_after_the_write_landed_keeps_the_tracking_claim(proxy, caplog):
    """The write landed and its record settled `done`, then marking the alert
    tracked failed on a transient store error. The guard must not release the
    claim: a retry is answered from the `done` record, and the grant keeps
    `tracking_op`, so the backstop's expiry notice says filing may not have
    finished rather than that no issue was filed."""

    class FlakyStore(FakeTrackingStore):
        def settle_tracked(self, *args, **kwargs):
            raise RuntimeError("dynamo throttled")

    store = FlakyStore(grant_row())
    proxy.github.file_issue.return_value = IssueCall(ok=True, status=201, body=CREATED)

    with patch.object(handler, "alert_store", return_value=store), caplog.at_level(logging.ERROR):
        first = route(issues_event(**FILE_ISSUE))
        retried = route(issues_event(**FILE_ISSUE))

    key = handler.issues.op_key({"tracking_id": "t-1", **FILE_ISSUE})
    assert status_of(first) == (502, {"status": "failed", "reason": "receiver crashed"})
    assert f"{observability.ISSUE_TRACKING_FAILED_MARKER} CHECKOUT-4B2 file_issue crashed" in caplog.text
    assert store.ops[key]["state"] == "done"
    assert store.grant["status"] == "open"
    assert store.grant.get("tracking_op") == key
    replay = done_body(retried)
    assert (replay["issue_number"], replay["replayed"]) == (9, True)
    assert proxy.github.file_issue.call_count == 1


def test_a_replayed_tracking_op_finishes_what_its_crash_left_undone(proxy):
    """The write landed and settled `done`, then marking the alert tracked
    failed once. The session's retry is answered from the record and also
    finishes the job, once: the alert is tracked with the created issue, so
    set_fields and add_to_project can follow, and the thread gets its link.
    The replay never shows the receiver's own resume data."""

    class SettleTrackedBlipsOnce(FakeTrackingStore):
        blipped = False

        def settle_tracked(self, *args, **kwargs):
            if not self.blipped:
                self.blipped = True
                raise RuntimeError("dynamo throttled")
            return super().settle_tracked(*args, **kwargs)

    store = SettleTrackedBlipsOnce(grant_row())
    proxy.github.file_issue.return_value = IssueCall(ok=True, status=201, body=CREATED)
    proxy.github.set_fields.return_value = FieldsOutcome(ok=True, fields={"Priority": "High"})

    with patch.object(handler, "alert_store", return_value=store):
        first = route(issues_event(**FILE_ISSUE))
        retried = route(issues_event(**FILE_ISSUE))
        resent = route(issues_event(**FILE_ISSUE))
        fields = route(issues_event(op="set_fields", issue_number=9, fields={"Priority": "High"}))

    assert first["statusCode"] == 502
    replay = done_body(retried)
    assert replay == {
        "status": "done", "op": "file_issue", "issue_number": 9,
        "url": CREATED["html_url"], "warnings": [], "replayed": True, "thread_notified": True,
    }
    assert store.grant["status"] == "tracked"
    assert (store.grant["created_issue"], store.grant["created_node_id"]) == (9, "I_9")
    assert proxy.bot.reply_in_thread.call_count == 1
    assert proxy.bot.reply_in_thread.call_args.args[2] == (
        "📌 Tracked in GitHub: https://github.com/acme-tools/checkout/issues/9"
    )
    assert done_body(resent)["thread_notified"] is False
    assert proxy.bot.reply_in_thread.call_count == 1
    assert done_body(fields)["fields"] == {"Priority": "High"}
    assert proxy.github.set_fields.call_args.kwargs["node_id"] == "I_9"


def test_issue_op_whose_write_landed_is_never_repeated_when_its_record_write_fails(proxy, caplog):
    """GitHub created the issue, then the `done` settle itself failed. The
    guard must not settle the still-`sending` record `failed` or release the
    claim, or the session's documented retry would file a second issue."""

    class DoneSettleFails(FakeTrackingStore):
        def settle_issue_op(self, tracking_id, key, state, *, result=None, error=""):
            if state == "done":
                raise RuntimeError("dynamo throttled")
            return super().settle_issue_op(tracking_id, key, state, result=result, error=error)

    store = DoneSettleFails(grant_row())
    proxy.github.file_issue.return_value = IssueCall(ok=True, status=201, body=CREATED)

    with patch.object(handler, "alert_store", return_value=store), caplog.at_level(logging.ERROR):
        first = route(issues_event(**FILE_ISSUE))
        retried = route(issues_event(**FILE_ISSUE))

    key = handler.issues.op_key({"tracking_id": "t-1", **FILE_ISSUE})
    assert status_of(first) == (502, {"status": "failed", "reason": "receiver crashed"})
    assert "file_issue crashed after its GitHub write landed" in caplog.text
    assert store.ops[key]["state"] == "sending"
    assert store.grant.get("tracking_op") == key
    assert status_of(retried) == (409, {"status": "refused", "reason": "in progress"})
    assert proxy.github.file_issue.call_count == 1


def test_issue_op_failure_whose_claim_release_blips_still_frees_the_alert(proxy):
    """GitHub wrote nothing, then releasing the claim failed once. The guard
    retries the release, so the session's documented retry without `type`
    and `labels`, a different op, can still claim the alert and file."""

    class ReleaseBlipsOnce(FakeTrackingStore):
        blipped = False

        def release_tracking_issue(self, tracking_id, key):
            if not self.blipped:
                self.blipped = True
                raise RuntimeError("dynamo throttled")
            return super().release_tracking_issue(tracking_id, key)

    store = ReleaseBlipsOnce(grant_row())
    proxy.github.file_issue.side_effect = [
        IssueCall(ok=False, status=503, step="create the issue"),
        IssueCall(ok=True, status=201, body={**CREATED, "labels": [], "type": None}),
    ]
    retry = {key: value for key, value in FILE_ISSUE.items() if key not in ("type", "labels")}

    with patch.object(handler, "alert_store", return_value=store):
        first = route(issues_event(**FILE_ISSUE))
        assert "tracking_op" not in store.grant
        second = route(issues_event(**retry))

    assert status_of(first) == (502, {"status": "failed", "reason": "receiver crashed"})
    assert done_body(second)["issue_number"] == 9
    assert store.grant["status"] == "tracked"
