"""GitHub App auth and the pull requests the receiver opens for autofix."""

from unittest.mock import MagicMock, patch

import pytest

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


BASE_SHA = "79bad4b79fb044dc6386fa690aae2bc3a6ebcc29"
BRANCH = "autofix/checkout-4b2-0f3c9a1e"
TITLE = "Autofix CHECKOUT-4B2: guard the empty-cart total"
PR_URL = f"https://github.com/{REPO}/pull/42"
FILES = [("src/cart.py", "total = 0\n"), ("tests/test_cart.py", "def test_total(): ...\n")]


def sequence(
    *, fail_get=None, fail_post=None, pull_body=None, compare_status="behind", tree_body=None
) -> MagicMock:
    """A session answering the mint and the eight-call sequence in order.

    GETs, zero-based: installation lookup, base ref, compare, base commit,
    base tree. POSTs: mint, one blob per file (two here), tree, commit,
    ref, pull. `fail_get`/`fail_post` make that call answer 500; `pull_body`
    replaces the final pull-request response body; `compare_status` is the
    compare call's `status` field; `tree_body` replaces the base tree
    listing's response body.
    """
    gets = [
        response(200, {"id": 77}),
        response(200, {"object": {"sha": "base-ref-sha"}}),
        response(200, {"status": compare_status}),
        response(200, {"sha": BASE_SHA, "tree": {"sha": "tree-base"}}),
        response(
            200,
            tree_body
            if tree_body is not None
            else {
                "truncated": False,
                "tree": [
                    {"path": "src/cart.py", "mode": "100755", "type": "blob"},
                    {"path": "tests/test_cart.py", "mode": "100644", "type": "blob"},
                ],
            },
        ),
    ]
    posts = [
        response(201, MINT_BODY),
        response(201, {"sha": "blob-1"}),
        response(201, {"sha": "blob-2"}),
        response(201, {"sha": "tree-new"}),
        response(201, {"sha": "commit-new"}),
        response(201, {"ref": f"refs/heads/{BRANCH}"}),
        response(201, {"html_url": PR_URL} if pull_body is None else pull_body),
    ]
    if fail_get is not None:
        gets[fail_get] = response(500)
    if fail_post is not None:
        posts[fail_post] = response(500)
    session = MagicMock()
    session.get.side_effect = gets
    session.post.side_effect = posts
    return session


def open_pr(session: MagicMock):
    return client_with(session).open_fix_pr(
        repo=REPO,
        base_sha=BASE_SHA,
        base_branch="develop",
        branch=BRANCH,
        files=FILES,
        title=TITLE,
        body="Root cause.",
    )


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_open_fix_pr_runs_the_six_calls_in_order_and_returns_the_url(encode):
    session = sequence()

    assert open_pr(session) == PR_URL

    base = f"https://api.github.com/repos/{REPO}"
    assert [c.args[0] for c in session.get.call_args_list] == [
        f"{base}/installation",
        f"{base}/git/ref/heads/develop",
        f"{base}/compare/develop...{BASE_SHA}",
        f"{base}/git/commits/{BASE_SHA}",
        f"{base}/git/trees/tree-base",
    ]
    assert session.get.call_args_list[4].kwargs["params"] == {"recursive": "1"}
    posts = session.post.call_args_list
    assert [c.args[0] for c in posts] == [
        "https://api.github.com/app/installations/77/access_tokens",
        f"{base}/git/blobs",
        f"{base}/git/blobs",
        f"{base}/git/trees",
        f"{base}/git/commits",
        f"{base}/git/refs",
        f"{base}/pulls",
    ]
    assert posts[1].kwargs["json"] == {"content": "total = 0\n", "encoding": "utf-8"}
    assert posts[3].kwargs["json"] == {
        "base_tree": "tree-base",
        "tree": [
            {"path": "src/cart.py", "mode": "100755", "type": "blob", "sha": "blob-1"},
            {"path": "tests/test_cart.py", "mode": "100644", "type": "blob", "sha": "blob-2"},
        ],
    }
    assert posts[4].kwargs["json"] == {
        "message": TITLE, "tree": "tree-new", "parents": [BASE_SHA],
    }
    assert posts[5].kwargs["json"] == {"ref": f"refs/heads/{BRANCH}", "sha": "commit-new"}
    assert posts[6].kwargs["json"] == {
        "title": TITLE, "body": "Root cause.", "head": BRANCH, "base": "develop", "draft": False,
    }
    # Every sequence call carries the minted token, never the App JWT.
    for call in session.get.call_args_list[1:] + posts[1:]:
        assert call.kwargs["headers"]["Authorization"] == "Bearer ghs_inst"


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_open_fix_pr_mints_exactly_the_autofix_permissions(encode):
    session = sequence()

    open_pr(session)

    mint = session.post.call_args_list[0]
    assert mint.kwargs["json"] == {
        "repositories": ["checkout"], "permissions": AUTOFIX_PERMISSIONS,
    }


@pytest.mark.parametrize("fail_get", [0, 1, 2, 3, 4])
@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_failed_read_returns_none_never_raises(encode, fail_get):
    assert open_pr(sequence(fail_get=fail_get)) is None


@pytest.mark.parametrize("fail_post", [0, 1, 2, 3, 4, 5, 6])
@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_failed_write_returns_none_never_raises(encode, fail_post):
    assert open_pr(sequence(fail_post=fail_post)) is None


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_pull_request_without_a_url_reads_as_not_opened(encode):
    assert open_pr(sequence(pull_body={})) is None


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_base_commit_off_the_base_branch_opens_nothing(encode):
    session = sequence(compare_status="diverged")

    assert open_pr(session) is None
    # The mint happened, but nothing past the compare check was ever posted.
    assert session.post.call_count == 1


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_truncated_base_tree_falls_back_to_regular_files(encode):
    session = sequence(tree_body={"truncated": True, "tree": []})

    assert open_pr(session) == PR_URL

    tree_call = session.post.call_args_list[3]
    assert [entry["mode"] for entry in tree_call.kwargs["json"]["tree"]] == [
        "100644",
        "100644",
    ]


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_every_request_carries_a_timeout_inside_the_budget(encode):
    session = sequence()

    assert open_pr(session) == PR_URL

    for call in session.get.call_args_list + session.post.call_args_list:
        timeout = call.kwargs["timeout"]
        assert 0 < timeout <= 15


@patch("receiver.github_app.time.monotonic", side_effect=[0.0] + [1000.0] * 50)
@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_an_exhausted_budget_stops_before_the_next_request(encode, monotonic):
    session = sequence()

    assert open_pr(session) is None
    assert session.get.call_count == 0
