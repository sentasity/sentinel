"""GitHub App auth: scoped installation-token minting."""

from unittest.mock import MagicMock, patch

from receiver.github_app import AUTOFIX_PERMISSIONS, GitHubAppClient

REPO = "acme-tools/checkout"


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


MINT_BODY = {"token": "ghs_inst", "expires_at": "2026-09-01T13:00:00Z"}


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_mint_looks_up_the_installation_then_posts_the_scoped_body(encode):
    session = MagicMock()
    session.get.return_value = response(200, {"id": 77})
    session.post.return_value = response(201, MINT_BODY)

    minted = client_with(session).mint_autofix_token(REPO)

    assert minted.token == "ghs_inst"
    assert minted.expires_at == "2026-09-01T13:00:00Z"
    lookup = session.get.call_args
    assert lookup.args[0] == f"https://api.github.com/repos/{REPO}/installation"
    assert lookup.kwargs["headers"]["Authorization"] == "Bearer app.jwt"
    mint = session.post.call_args
    assert mint.args[0] == "https://api.github.com/app/installations/77/access_tokens"
    assert mint.kwargs["json"] == {
        "repositories": ["checkout"],
        "permissions": AUTOFIX_PERMISSIONS,
    }


def test_the_autofix_grant_never_includes_workflows():
    # A workflow-file change runs in CI with secrets access before review;
    # the scope is the enforcement, so this invariant gets its own test.
    assert AUTOFIX_PERMISSIONS == {"contents": "write", "pull_requests": "write"}
    assert "workflows" not in AUTOFIX_PERMISSIONS


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_the_app_jwt_is_signed_rs256_with_the_app_id(encode):
    session = MagicMock()
    session.get.return_value = response(200, {"id": 77})
    session.post.return_value = response(201, MINT_BODY)

    client_with(session).mint_autofix_token(REPO)

    claims = encode.call_args.args[0]
    assert claims["iss"] == "1234567"
    assert encode.call_args.kwargs["algorithm"] == "RS256"


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_failed_mint_returns_none_never_raises(encode):
    session = MagicMock()
    session.get.return_value = response(200, {"id": 77})
    session.post.return_value = response(422)

    assert client_with(session).mint_autofix_token(REPO) is None


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_transport_error_returns_none_never_raises(encode):
    session = MagicMock()
    session.get.side_effect = OSError("connection reset")

    assert client_with(session).mint_autofix_token(REPO) is None


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_2xx_body_missing_the_token_key_returns_none_never_raises(encode):
    session = MagicMock()
    session.get.return_value = response(200, {"id": 77})
    session.post.return_value = response(201, {})

    assert client_with(session).mint_autofix_token(REPO) is None


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_the_app_slug_is_fetched_once_and_cached(encode):
    session = MagicMock()
    session.get.return_value = response(200, {"slug": "acme-autofix"})
    client = client_with(session)

    assert client.app_slug() == "acme-autofix"
    assert client.app_slug() == "acme-autofix"

    assert session.get.call_count == 1
    assert session.get.call_args.args[0] == "https://api.github.com/app"


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_failed_slug_fetch_returns_empty_and_is_not_cached(encode):
    session = MagicMock()
    session.get.side_effect = [OSError("reset"), response(200, {"slug": "acme-autofix"})]
    client = client_with(session)

    assert client.app_slug() == ""
    assert client.app_slug() == "acme-autofix"


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_pr_author_reads_with_a_read_only_token(encode):
    session = MagicMock()
    session.get.side_effect = [
        response(200, {"id": 77}),                        # installation lookup
        response(200, {"user": {"login": "acme-autofix[bot]"}}),  # the PR
    ]
    session.post.return_value = response(201, {"token": "ghs_read"})

    author = client_with(session).pr_author(REPO, 42)

    assert author == "acme-autofix[bot]"
    mint = session.post.call_args
    assert mint.kwargs["json"]["permissions"] == {"pull_requests": "read"}
    fetch = session.get.call_args_list[1]
    assert fetch.args[0] == f"https://api.github.com/repos/{REPO}/pulls/42"
    assert fetch.kwargs["headers"]["Authorization"] == "Bearer ghs_read"


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_pr_author_returns_empty_on_any_failure(encode):
    session = MagicMock()
    session.get.side_effect = OSError("reset")

    assert client_with(session).pr_author(REPO, 42) == ""
