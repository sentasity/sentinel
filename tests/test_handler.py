"""Function URL routing, HMAC verification, and the alert pipeline."""

import base64
import copy
import hashlib
import hmac
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from receiver import handler, observability
from receiver.config import ReceiverConfig
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
    sentry_org="sentasity",
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


def test_warning_alert_posts_to_prod_channel_with_the_footer():
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
    assert card["body"][-1]["text"] == "⚠️ Warnings are not auto-investigated."


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
    """A session retrying its POST must not double-post the thread reply."""
    store = MagicMock()
    store.claim_batch.return_value = (BatchState.DELIVERED, [])

    with patch.object(handler, "config", return_value=CONFIG), \
         patch.object(handler, "alert_store", return_value=store), \
         patch.object(handler, "deliver_findings") as deliver:
        response = handler.lambda_handler(findings_request("tok"), None)

    assert response["statusCode"] == 200
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


def autofix_row():
    return {
        "issue_id": "1000000007",
        "environment": "staging",
        "release": "79bad4b79fb044dc6386fa690aae2bc3a6ebcc29",
        "batch_id": "6f1d2c88-0a2b-4f77-9d31-8f0d6a7c1e42",
        "project": "checkout",
        "short_id": "CHECKOUT-4B2",
        "conversation_id": "conv-1",
        "message_id": "msg-9",
    }


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_passing_finding_vends_a_token_and_says_so_in_the_thread(
    config, alert_store, bot_client, github_client, tmp_path
):
    from receiver.github_app import MintedToken
    from receiver.store import AlertStore
    from tests.test_config import AUTOFIX, VALID, write
    from receiver.config import load_config

    cfg = load_config(write(tmp_path, VALID + AUTOFIX))
    config.return_value = cfg
    store = alert_store.return_value
    store.advance.return_value = True
    store.claim_autofix_dedupe.return_value = True
    store.claim_autofix_pr.return_value = True
    github_client.return_value.mint_autofix_token.return_value = MintedToken(
        token="ghs_vended", expires_at="2026-09-01T13:00:00Z"
    )

    response = deliver_findings(load_fixture("findings-payload-v2.json"), [autofix_row()])

    assert response["statusCode"] == 200
    assert response["headers"]["content-type"] == "application/json"
    block = json.loads(response["body"])["autofix"]
    # The key set is a contract with the session prompt, which reads these
    # names verbatim, so pin the whole shape rather than the fields in use.
    assert set(block) == {
        "repo",
        "base_branch",
        "github_token",
        "github_token_expires_at",
        "callback_url",
        "grants",
    }
    assert block["repo"] == cfg.target_repo
    assert block["base_branch"] == cfg.autofix_base_branch
    assert block["github_token"] == "ghs_vended"
    assert block["github_token_expires_at"] == "2026-09-01T13:00:00Z"
    assert block["callback_url"] == cfg.autofix_callback_url
    assert github_client.return_value.mint_autofix_token.call_args.args == (
        cfg.target_repo,
    )

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

    response = deliver_findings(load_fixture("findings-payload-v2.json"), [autofix_row()])

    assert json.loads(response["body"]) == {"autofix": None}
    card = bot_client.return_value.reply_card_in_thread.call_args.args[2]
    assert "Autofix declined: project not opted in." in json.dumps(card)
    github_client.return_value.mint_autofix_token.assert_not_called()
    store.put_autofix_dispatch.assert_not_called()


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_mint_failure_withdraws_the_grant_and_declines_in_the_thread(
    config, alert_store, bot_client, github_client, tmp_path, caplog
):
    from tests.test_config import AUTOFIX, VALID, write
    from receiver.config import load_config

    config.return_value = load_config(write(tmp_path, VALID + AUTOFIX))
    store = alert_store.return_value
    store.advance.return_value = True
    store.claim_autofix_dedupe.return_value = True
    store.claim_autofix_pr.return_value = True
    github_client.return_value.mint_autofix_token.return_value = None

    with caplog.at_level(logging.ERROR):
        response = deliver_findings(load_fixture("findings-payload-v2.json"), [autofix_row()])

    assert json.loads(response["body"]) == {"autofix": None}
    store.advance_autofix.assert_called_once()
    assert store.advance_autofix.call_args.args[1:] == ("dispatched", "failed")
    assert (
        store.advance_autofix.call_args.args[0]
        == store.put_autofix_dispatch.call_args.args[0]["dispatch_id"]
    )
    card = bot_client.return_value.reply_card_in_thread.call_args.args[2]
    assert "could not mint a GitHub credential" in json.dumps(card)
    # The thread must never read "attempting" when no credential exists, which
    # is only true if the cards post after the mint, not before it.
    assert "attempting a fix in this session" not in json.dumps(card)
    assert observability.AUTOFIX_FAILED_MARKER in caplog.text


@patch("receiver.handler.github_client")
@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
@patch("receiver.handler.config")
def test_a_failed_thread_post_still_returns_the_grant(
    config, alert_store, bot_client, github_client, tmp_path
):
    """A chat outage is not a reason to abandon the fix: the findings survived
    validation, the credential exists, and the reply goes to the retry sweep."""
    from receiver.github_app import MintedToken
    from tests.test_config import AUTOFIX, VALID, write
    from receiver.config import load_config

    config.return_value = load_config(write(tmp_path, VALID + AUTOFIX))
    store = alert_store.return_value
    store.advance.return_value = True
    store.claim_autofix_dedupe.return_value = True
    store.claim_autofix_pr.return_value = True
    github_client.return_value.mint_autofix_token.return_value = MintedToken(
        token="ghs_vended", expires_at="2026-09-01T13:00:00Z"
    )
    bot_client.return_value.reply_card_in_thread.side_effect = BotError("teams down")

    response = deliver_findings(load_fixture("findings-payload-v2.json"), [autofix_row()])

    block = json.loads(response["body"])["autofix"]
    assert block["github_token"] == "ghs_vended"
    assert len(block["grants"]) == 1
    store.advance_autofix.assert_not_called()


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

    response = deliver_findings(load_fixture("findings-payload-v2.json"), [autofix_row()])

    card = bot_client.return_value.reply_card_in_thread.call_args.args[2]
    assert "Autofix" not in json.dumps(card)
    assert json.loads(response["body"]) == {"autofix": None}
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
        response = deliver_findings(load_fixture("findings-payload-v2.json"), [autofix_row()])

    assert response["statusCode"] == 200
    bot_client.return_value.reply_card_in_thread.assert_called_once()
    card = bot_client.return_value.reply_card_in_thread.call_args.args[2]
    assert "Autofix" not in json.dumps(card)
    assert json.loads(response["body"]) == {"autofix": None}
    github_client.return_value.mint_autofix_token.assert_not_called()
    assert observability.AUTOFIX_FAILED_MARKER in caplog.text


from receiver.handler import route


def callback_event(token="cb-token", **body):
    payload = {"dispatch_id": "d-1", "status": "pr_opened", "pr_url": "https://pr", **body}
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
def test_a_pr_opened_callback_replies_with_the_link(alert_store, bot_client):
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    response = route(callback_event())

    assert response["statusCode"] == 200
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert reply == "Autofix PR opened: https://pr"
    assert store.advance_autofix.call_args.args == ("d-1", "dispatched", "pr_opened")


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
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert "https://run" in reply


def test_autofix_result_without_a_token_is_401():
    assert route(callback_event(token=None))["statusCode"] == 401


def test_autofix_result_with_a_non_ascii_token_is_rejected_not_crashed():
    assert route(callback_event(token="tökén"))["statusCode"] == 401


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_malicious_pr_url_is_rejected_not_relayed_into_the_reply(alert_store, bot_client):
    """A masked-link payload must not reach Teams as clickable markdown."""
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    payload = "https://good.example/x)[Click to review](https://phish.example"
    response = route(callback_event(pr_url=payload))

    assert response["statusCode"] == 200
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert "phish.example" not in reply
    assert "[Click to review]" not in reply
    assert reply == "Autofix PR opened: (missing PR URL)"


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_legitimate_pr_url_survives_intact_and_stays_a_link(alert_store, bot_client):
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    good_url = "https://github.com/acme-tools/checkout/pull/42"
    response = route(callback_event(pr_url=good_url))

    assert response["statusCode"] == 200
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert reply == f"Autofix PR opened: {good_url}"


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_bold_markdown_pr_url_is_neutralized_not_relayed(alert_store, bot_client):
    """`*` passes CALLBACK_URL_RE's shape check but is bold in Teams; a
    domain-spoofing payload must not reach the reply as live `**...**`."""
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    payload = "https://evil.example/**not-really-github.com**"
    response = route(callback_event(pr_url=payload))

    assert response["statusCode"] == 200
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert "**" not in reply


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_an_underscore_markdown_pr_url_is_neutralized_not_relayed(alert_store, bot_client):
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    payload = "https://evil.example/_not-really-github.com_"
    response = route(callback_event(pr_url=payload))

    assert response["statusCode"] == 200
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert "_" not in reply


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_backslash_pr_url_is_neutralized_not_relayed(alert_store, bot_client):
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    payload = "https://evil.example/\\not-really-github.com\\"
    response = route(callback_event(pr_url=payload))

    assert response["statusCode"] == 200
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert "\\" not in reply


@patch("receiver.handler.bot_client")
@patch("receiver.handler.alert_store")
def test_a_legitimate_underscored_pr_url_still_produces_a_working_link(
    alert_store, bot_client
):
    """`_` is common in real GitHub repo/branch names and must not be
    rejected outright; percent-encoding keeps the link real and clickable."""
    store = alert_store.return_value
    store.get_autofix_dispatch.return_value = dispatch_record()
    store.advance_autofix.return_value = True

    good_url = "https://github.com/sentasity/my_repo/pull/42"
    response = route(callback_event(pr_url=good_url))

    assert response["statusCode"] == 200
    reply = bot_client.return_value.reply_in_thread.call_args.args[2]
    assert "https://github.com/" in reply
    assert "(missing PR URL)" not in reply
    assert reply.count("https://") == 1
