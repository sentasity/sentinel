"""Bot Framework REST client used to post cards and thread replies."""

from unittest.mock import MagicMock, patch

import pytest

from receiver.bot import BotError, TeamsBotClient


def make_client():
    return TeamsBotClient(
        tenant_id="tenant-123",
        app_id="app-456",
        app_password="s3cret",
        service_url="https://smba.trafficmanager.net/amer/",
    )


def response(status_code, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = payload or {}
    resp.text = "body"
    return resp


def test_token_request_uses_the_tenant_endpoint_and_bot_scope():
    client = make_client()

    with patch.object(client.session, "post", return_value=response(200, {"access_token": "tok"})) as post:
        assert client.token() == "tok"

    url, = post.call_args.args
    assert url == "https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token"
    assert post.call_args.kwargs["data"] == {
        "grant_type": "client_credentials",
        "client_id": "app-456",
        "client_secret": "s3cret",
        "scope": "https://api.botframework.com/.default",
    }


def test_token_is_cached_between_calls():
    client = make_client()

    with patch.object(client.session, "post", return_value=response(200, {"access_token": "tok"})) as post:
        client.token()
        client.token()

    assert post.call_count == 1


def test_token_failure_raises_bot_error():
    client = make_client()

    with patch.object(client.session, "post", return_value=response(401)):
        with pytest.raises(BotError, match="token request failed"):
            client.token()


def test_post_card_targets_the_channel_and_returns_ids():
    client = make_client()
    client._token = "tok"
    created = response(201, {"id": "conv-1;messageid=msg-9", "activityId": "msg-9"})

    with patch.object(client.session, "post", return_value=created) as post:
        conversation_id, message_id = client.post_card(
            "19:chan@thread.tacv2", {"type": "AdaptiveCard"}, "summary line"
        )

    url, = post.call_args.args
    payload = post.call_args.kwargs["json"]
    assert url == "https://smba.trafficmanager.net/amer/v3/conversations"
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"
    assert payload["isGroup"] is True
    assert payload["channelData"]["channel"]["id"] == "19:chan@thread.tacv2"
    assert payload["activity"]["summary"] == "summary line"
    assert payload["activity"]["attachments"][0]["contentType"] == (
        "application/vnd.microsoft.card.adaptive"
    )
    assert (conversation_id, message_id) == ("conv-1;messageid=msg-9", "msg-9")


def test_post_card_raises_on_http_error():
    client = make_client()
    client._token = "tok"

    with patch.object(client.session, "post", return_value=response(502)):
        with pytest.raises(BotError, match="conversation create failed"):
            client.post_card("19:chan@thread.tacv2", {}, "summary")


def test_reply_in_thread_addresses_the_parent_message():
    client = make_client()
    client._token = "tok"

    with patch.object(client.session, "post", return_value=response(201, {"id": "act-2"})) as post:
        activity_id = client.reply_in_thread("conv-1", "msg-9", "findings here")

    url, = post.call_args.args
    assert url == (
        "https://smba.trafficmanager.net/amer/v3/conversations/"
        "conv-1;messageid=msg-9/activities"
    )
    assert post.call_args.kwargs["json"] == {"type": "message", "text": "findings here"}
    assert activity_id == "act-2"


def test_reply_in_thread_raises_on_http_error():
    client = make_client()
    client._token = "tok"

    with patch.object(client.session, "post", return_value=response(404)):
        with pytest.raises(BotError, match="thread reply failed"):
            client.reply_in_thread("conv-1", "msg-9", "findings here")


def test_reply_card_in_thread_attaches_the_card_to_the_parent_message():
    client = make_client()
    client._token = "tok"

    with patch.object(client.session, "post", return_value=response(201, {"id": "act-2"})) as post:
        activity_id = client.reply_card_in_thread(
            "conv-1", "msg-9", {"type": "AdaptiveCard"}, "summary line"
        )

    url, = post.call_args.args
    payload = post.call_args.kwargs["json"]
    assert url == (
        "https://smba.trafficmanager.net/amer/v3/conversations/"
        "conv-1;messageid=msg-9/activities"
    )
    assert payload["summary"] == "summary line"
    assert payload["attachments"][0]["contentType"] == (
        "application/vnd.microsoft.card.adaptive"
    )
    assert payload["attachments"][0]["content"] == {"type": "AdaptiveCard"}
    assert activity_id == "act-2"


def test_reply_card_in_thread_raises_on_http_error():
    client = make_client()
    client._token = "tok"

    with patch.object(client.session, "post", return_value=response(502)):
        with pytest.raises(BotError, match="thread card reply failed"):
            client.reply_card_in_thread("conv-1", "msg-9", {}, "summary")


def test_token_is_refetched_once_it_expires():
    client = make_client()
    expiring = response(200, {"access_token": "tok", "expires_in": 0})

    with patch.object(client.session, "post", return_value=expiring) as post:
        client.token()
        client.token()

    assert post.call_count == 2


def test_post_card_refreshes_the_token_and_retries_once_on_401():
    """A warm container holding an expired token must recover, not 500 forever."""
    client = make_client()
    client._token = "stale"
    created = response(201, {"id": "conv-1", "activityId": "msg-9"})

    with patch.object(
        client.session,
        "post",
        side_effect=[response(401), response(200, {"access_token": "fresh"}), created],
    ) as post:
        assert client.post_card("19:chan@thread.tacv2", {}, "summary") == (
            "conv-1",
            "msg-9",
        )

    assert post.call_count == 3
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh"


def test_post_card_gives_up_after_one_retry():
    client = make_client()
    client._token = "stale"

    with patch.object(
        client.session,
        "post",
        side_effect=[response(401), response(200, {"access_token": "fresh"}), response(401)],
    ):
        with pytest.raises(BotError, match="conversation create failed"):
            client.post_card("19:chan@thread.tacv2", {}, "summary")


def test_reply_in_thread_also_retries_once_on_401():
    client = make_client()
    client._token = "stale"

    with patch.object(
        client.session,
        "post",
        side_effect=[
            response(401),
            response(200, {"access_token": "fresh"}),
            response(201, {"id": "act-2"}),
        ],
    ):
        assert client.reply_in_thread("conv-1", "msg-9", "findings here") == "act-2"


def test_an_unparseable_expires_in_does_not_mint_an_immortal_token():
    """A malformed AAD response must not resurrect the cache-forever bug."""
    client = make_client()
    garbled = response(200, {"access_token": "tok", "expires_in": "not-a-number"})

    with patch.object(client.session, "post", return_value=garbled) as post:
        client.token()
        client.token()

    # Falls back to the default lifetime rather than an unbounded cache, and
    # never leaves the client without a usable expiry.
    assert client._token == "tok"
    assert client._token_expires_at is not None
    assert post.call_count == 1
