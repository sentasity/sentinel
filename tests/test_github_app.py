"""GitHub App auth and the repository_dispatch client."""

from unittest.mock import MagicMock, patch

from receiver.github_app import DispatchOutcome, GitHubAppClient

REPO = "sentasity/sentinel"


def response(status: int, body: dict | None = None):
    mock = MagicMock()
    mock.status_code = status
    mock.ok = status < 400
    mock.json.return_value = body or {}
    mock.text = ""
    mock.raise_for_status.side_effect = None if status < 400 else Exception("boom")
    return mock


def client_with(session: MagicMock) -> GitHubAppClient:
    client = GitHubAppClient("1234567", "-----BEGIN RSA PRIVATE KEY-----fake")
    client.session = session
    return client


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_dispatch_mints_an_installation_token_then_posts(encode):
    session = MagicMock()
    session.get.return_value = response(200, {"id": 77})
    session.post.side_effect = [
        response(201, {"token": "ghs_inst"}),
        response(204),
    ]

    outcome = client_with(session).dispatch(REPO, "autofix", {"dispatch_id": "d-1"})

    assert outcome is DispatchOutcome.DISPATCHED
    lookup = session.get.call_args
    assert lookup.args[0] == f"https://api.github.com/repos/{REPO}/installation"
    assert lookup.kwargs["headers"]["Authorization"] == "Bearer app.jwt"
    mint, fire = session.post.call_args_list
    assert mint.args[0] == "https://api.github.com/app/installations/77/access_tokens"
    assert fire.args[0] == f"https://api.github.com/repos/{REPO}/dispatches"
    assert fire.kwargs["headers"]["Authorization"] == "Bearer ghs_inst"
    assert fire.kwargs["json"]["event_type"] == "autofix"


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_the_app_jwt_is_signed_rs256_with_the_app_id(encode):
    session = MagicMock()
    session.get.return_value = response(200, {"id": 77})
    session.post.side_effect = [response(201, {"token": "t"}), response(204)]

    client_with(session).dispatch(REPO, "autofix", {})

    claims = encode.call_args.args[0]
    assert claims["iss"] == "1234567"
    assert encode.call_args.kwargs["algorithm"] == "RS256"


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_5xx_dispatch_is_retryable(encode):
    session = MagicMock()
    session.get.return_value = response(200, {"id": 77})
    session.post.side_effect = [response(201, {"token": "t"}), response(502)]

    assert client_with(session).dispatch(REPO, "autofix", {}) is DispatchOutcome.RETRYABLE


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_4xx_dispatch_is_rejected(encode):
    session = MagicMock()
    session.get.return_value = response(200, {"id": 77})
    session.post.side_effect = [response(201, {"token": "t"}), response(422)]

    assert client_with(session).dispatch(REPO, "autofix", {}) is DispatchOutcome.REJECTED


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_transport_error_is_retryable_never_raised(encode):
    session = MagicMock()
    session.get.side_effect = OSError("connection reset")

    assert client_with(session).dispatch(REPO, "autofix", {}) is DispatchOutcome.RETRYABLE
