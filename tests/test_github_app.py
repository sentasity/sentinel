"""GitHub App auth and the pull requests the receiver opens for autofix."""

import base64
import os
import re
from unittest.mock import MagicMock, patch

import pytest

from receiver import autofix

from receiver.github_app import (
    AUTOFIX_PERMISSIONS,
    ISSUE_FIELDS_PERMISSION,
    GitHubAppClient,
    PullRequestOutcome,
)
from receiver.patch import blob_sha, parse_patch
from tests.gitrepo import Repo

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


def test_the_issue_fields_permission_is_a_github_permission_key():
    """Read back from the approved installation rather than guessed. GitHub's
    permission keys are lowercase snake_case, and this one is none of the
    other permissions an issue op or a fix mints."""
    assert re.fullmatch(r"[a-z]+(?:_[a-z]+)*", ISSUE_FIELDS_PERMISSION)
    assert "issue" in ISSUE_FIELDS_PERMISSION
    assert ISSUE_FIELDS_PERMISSION not in {
        "issues", "organization_projects", "contents", "pull_requests", "workflows",
    }


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
        commit_message=f"{TITLE}\n\nFixes CHECKOUT-4B2",
    )


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_open_fix_pr_runs_the_six_calls_in_order_and_returns_the_url(encode):
    session = sequence()

    assert open_pr(session).url == PR_URL

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
        "message": f"{TITLE}\n\nFixes CHECKOUT-4B2", "tree": "tree-new", "parents": [BASE_SHA],
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
def test_a_failed_read_opens_nothing_never_raises(encode, fail_get):
    assert open_pr(sequence(fail_get=fail_get)) == PullRequestOutcome()


@pytest.mark.parametrize("fail_post", [0, 1, 2, 3, 4, 5, 6])
@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_failed_write_opens_nothing_never_raises(encode, fail_post):
    assert open_pr(sequence(fail_post=fail_post)) == PullRequestOutcome()


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_pull_request_without_a_url_reads_as_not_opened(encode):
    assert open_pr(sequence(pull_body={})) == PullRequestOutcome()


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_base_commit_off_the_base_branch_opens_nothing(encode):
    session = sequence(compare_status="diverged")

    assert open_pr(session) == PullRequestOutcome()
    # The mint happened, but nothing past the compare check was ever posted.
    assert session.post.call_count == 1


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_every_request_carries_a_timeout_inside_the_budget(encode):
    session = sequence()

    assert open_pr(session).url == PR_URL

    for call in session.get.call_args_list + session.post.call_args_list:
        timeout = call.kwargs["timeout"]
        assert 0 < timeout <= 15


@patch("receiver.github_app.time.monotonic", side_effect=[0.0] + [1000.0] * 50)
@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_an_exhausted_budget_stops_before_the_next_request(encode, monotonic):
    session = sequence()

    assert open_pr(session) == PullRequestOutcome()
    assert session.get.call_count == 0


# --- Against a real repository ---------------------------------------------
#
# The ordered mock above pins the call sequence. These answer by URL from a
# real repository's objects instead, so they can check the one thing that
# matters about a patch: the tree the receiver builds is the tree git built.


class FakeGitHub:
    """Answers the receiver's GitHub calls from `repo` at `base`, keeping
    what it is sent. `truncated` makes the recursive listing come back the
    way GitHub returns it for a tree past its listing limit."""

    def __init__(self, repo: Repo, base: str, *, truncated: bool = False):
        self.repo, self.base, self.truncated = repo, base, truncated
        self.uploaded: dict[str, bytes] = {}
        self.tree_request: dict | None = None
        self.calls: list[tuple[str, str]] = []
        self.api = f"https://api.github.com/repos/{REPO}"

    def _listing(self, sha: str, recursive: bool) -> list[dict]:
        args = ["ls-tree", "-l", "-z"] + (["-r", "-t"] if recursive else []) + [sha]
        entries = []
        for line in self.repo.git(*args).split(b"\0"):
            if not line:
                continue
            meta, path = line.split(b"\t", 1)
            mode, kind, sha_, size = meta.decode().split()
            entry = {"path": path.decode(), "mode": mode, "type": kind, "sha": sha_}
            if kind == "blob":
                entry["size"] = int(size)
            entries.append(entry)
        return entries

    def get(self, url, headers=None, timeout=None, params=None):
        self.calls.append(("get", url))
        tree_sha = self.repo.git("rev-parse", f"{self.base}^{{tree}}").decode().strip()
        if url.endswith("/installation"):
            return response(200, {"id": 77})
        if "/git/ref/heads/" in url:
            return response(200, {"object": {"sha": self.base}})
        if "/compare/" in url:
            return response(200, {"status": "identical"})
        if url == f"{self.api}/git/commits/{self.base}":
            return response(200, {"sha": self.base, "tree": {"sha": tree_sha}})
        if "/git/trees/" in url:
            sha = url.rsplit("/", 1)[1]
            recursive = bool(params and params.get("recursive"))
            if recursive and self.truncated:
                return response(200, {"truncated": True, "tree": []})
            return response(200, {"truncated": False, "tree": self._listing(sha, recursive)})
        if "/git/blobs/" in url:
            assert headers["Accept"] == "application/vnd.github.raw+json"
            got = response(200)
            got.content = self.repo.git("cat-file", "blob", url.rsplit("/", 1)[1])
            return got
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, headers=None, timeout=None, json=None):
        self.calls.append(("post", url))
        if url.endswith("/access_tokens"):
            return response(201, MINT_BODY)
        if url.endswith("/git/blobs"):
            if json["encoding"] == "base64":
                content = base64.b64decode(json["content"])
            else:
                content = json["content"].encode("utf-8")
            sha = blob_sha(content)
            self.uploaded[sha] = content
            return response(201, {"sha": sha})
        if url.endswith("/git/trees"):
            self.tree_request = json
            return response(201, {"sha": "tree-new"})
        if url.endswith("/git/commits"):
            return response(201, {"sha": "commit-new"})
        if url.endswith("/git/refs"):
            return response(201, {})
        if url.endswith("/pulls"):
            return response(201, {"html_url": PR_URL})
        raise AssertionError(f"unexpected POST {url}")

    def resulting_tree(self) -> dict[str, tuple[str, str]]:
        """The base tree with the receiver's create-tree request applied."""
        tree = {path: (mode, sha) for path, (mode, sha, _) in self.repo.tree(self.base).items()}
        for entry in self.tree_request["tree"]:
            if entry["sha"] is None:
                del tree[entry["path"]]
            else:
                tree[entry["path"]] = (entry["mode"], entry["sha"])
        return tree


def working_tree(repo: Repo) -> dict[str, tuple[str, str]]:
    """What git itself makes of the working tree: path -> (mode, blob)."""
    repo.git("add", "-A")
    tree = repo.git("write-tree").decode().strip()
    return {path: (mode, sha) for path, (mode, sha, _) in repo.tree(tree).items()}


def open_against(fake: FakeGitHub, *, files=(), changes=()) -> PullRequestOutcome:
    client = client_with(MagicMock())
    client.session = fake
    return client.open_fix_pr(
        repo=REPO,
        base_sha=fake.base,
        base_branch="develop",
        branch=BRANCH,
        files=list(files),
        changes=list(changes),
        title=TITLE,
        body="Root cause.",
        commit_message=TITLE,
    )


def changes_of(data: bytes):
    return parse_patch(data, max_binary_bytes=autofix.FIX_REWRITE_BYTES_LIMIT)


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "repo")


def lines(count: int, changed: dict[int, str] | None = None) -> str:
    changed = changed or {}
    return "".join(changed.get(i, f'  "row {i}": {i},') + "\n" for i in range(count))


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_patch_builds_exactly_the_tree_git_built(encode, repo):
    repo.write("src/cart.py", "total = None\n")
    repo.write("src/legacy.py", "old = 1\n")
    repo.write("src/rename_me.py", "moved = 1\n" * 10)
    repo.write("bin/run.sh", "echo hi\n")
    repo.write("fixtures/demo.json", lines(150_000))
    base = repo.commit()
    repo.write("src/cart.py", "total = 0\n")
    repo.write("tests/test_cart.py", "def test_total(): ...\n")
    repo.remove("src/legacy.py")
    repo.move("src/rename_me.py", "src/renamed.py")
    (repo.root / "bin/run.sh").chmod(0o755)
    repo.write("fixtures/demo.json", lines(150_000, {10: '  "row 10": 0,', 90_000: '  "x": 1,'}))
    data = repo.patch(base)
    fake = FakeGitHub(repo, base)

    outcome = open_against(fake, changes=changes_of(data))

    assert outcome == PullRequestOutcome(url=PR_URL)
    assert fake.resulting_tree() == working_tree(repo)
    assert fake.tree_request["base_tree"] == repo.git(
        "rev-parse", f"{base}^{{tree}}"
    ).decode().strip()
    # What crossed the callback is the change; only the receiver moves the
    # large file, and only to GitHub.
    assert len(base64.b64encode(data)) < 4_000
    assert len(repo.read("fixtures/demo.json")) > 2_000_000


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_rename_or_mode_change_reuses_the_base_blob(encode, repo):
    repo.write("src/rename_me.py", "moved = 1\n" * 10)
    repo.write("bin/run.sh", "echo hi\n")
    base = repo.commit()
    repo.move("src/rename_me.py", "src/renamed.py")
    (repo.root / "bin/run.sh").chmod(0o755)
    fake = FakeGitHub(repo, base)

    outcome = open_against(fake, changes=changes_of(repo.patch(base)))

    assert outcome.url == PR_URL
    assert fake.uploaded == {}
    assert not [url for method, url in fake.calls if "/git/blobs/" in url]
    assert fake.resulting_tree() == working_tree(repo)


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_patch_made_against_another_commit_opens_nothing(encode, repo):
    repo.write("src/cart.py", lines(40))
    older = repo.commit()
    repo.write("src/cart.py", lines(40, {30: "drift"}))
    newer = repo.commit()
    repo.write("src/cart.py", lines(40, {30: "drift", 5: "fix"}))
    data = repo.patch(newer)
    fake = FakeGitHub(repo, older)

    outcome = open_against(fake, changes=changes_of(data))

    assert outcome.url == ""
    assert "src/cart.py differs at base_sha" in outcome.failure
    assert fake.tree_request is None
    assert not [url for method, url in fake.calls if method == "post" and "/git/" in url]


def patch_from_elsewhere(tmp_path, edit) -> bytes:
    """A patch made in another repository, whose tree the receiver's base
    does not match."""
    other = Repo(tmp_path / "other")
    other.write("src/cart.py", "x\n")
    base = other.commit()
    edit(other)
    return other.patch(base)


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_patch_creating_a_path_the_base_already_has_opens_nothing(encode, repo, tmp_path):
    data = patch_from_elsewhere(tmp_path, lambda other: other.write("src/new.py", "y\n"))
    repo.write("src/new.py", "already here\n")
    base = repo.commit()

    outcome = open_against(FakeGitHub(repo, base), changes=changes_of(data))

    assert outcome == PullRequestOutcome(failure="src/new.py already exists at base_sha")


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_patch_editing_a_path_the_base_lacks_opens_nothing(encode, repo, tmp_path):
    data = patch_from_elsewhere(tmp_path, lambda other: other.write("src/cart.py", "y\n"))
    repo.write("src/other.py", "x\n")
    base = repo.commit()

    outcome = open_against(FakeGitHub(repo, base), changes=changes_of(data))

    assert outcome == PullRequestOutcome(failure="src/cart.py does not exist at base_sha")


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_patch_cannot_rename_a_symlink_the_patch_never_declared(encode, repo):
    """A pure rename carries no mode, so only the base tree can say the
    path is a symlink."""
    repo.write("src/cart.py", "x\n")
    os.symlink("src/cart.py", repo.root / "link")
    base = repo.commit()
    repo.move("link", "link2")
    changes = changes_of(repo.patch(base))
    assert changes[0].old_mode is None

    outcome = open_against(FakeGitHub(repo, base), changes=changes)

    assert outcome.failure == "link is a symlink at base_sha"


@pytest.mark.parametrize(
    ("path", "rule"),
    [
        ("link", "link is a symlink at base_sha"),
        ("link/inner.py", "link/inner.py runs through link, which is not a directory at base_sha"),
        ("src", "src is a directory at base_sha"),
    ],
)
@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_whole_files_obey_the_same_base_tree_policy(encode, repo, path, rule):
    repo.write("src/cart.py", "x\n")
    os.symlink("src/cart.py", repo.root / "link")
    base = repo.commit()
    fake = FakeGitHub(repo, base)

    outcome = open_against(fake, files=[(path, "content\n")])

    assert outcome == PullRequestOutcome(failure=rule)
    assert fake.tree_request is None


@pytest.mark.parametrize("form", ["files", "patch"])
@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_truncated_listing_reads_only_the_fixs_directories(encode, repo, form):
    repo.write("src/cart.py", "total = None\n", executable=True)
    repo.write("src/deep/er/x.py", "x\n")
    repo.write("docs/unrelated.md", "y\n")
    base = repo.commit()
    repo.write("src/cart.py", "total = 0\n", executable=True)
    repo.write("src/deep/er/x.py", "y\n")
    fake = FakeGitHub(repo, base, truncated=True)

    if form == "files":
        outcome = open_against(
            fake, files=[("src/cart.py", "total = 0\n"), ("src/deep/er/x.py", "y\n")]
        )
    else:
        outcome = open_against(fake, changes=changes_of(repo.patch(base)))

    assert outcome.url == PR_URL
    assert fake.resulting_tree() == working_tree(repo)
    listings = [url for method, url in fake.calls if "/git/trees/" in url and method == "get"]
    # The truncated recursive listing, then root, src, src/deep, src/deep/er:
    # never docs/.
    assert len(listings) == 5


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_patch_that_would_rewrite_too_much_opens_nothing(encode, repo, monkeypatch):
    repo.write("fixtures/demo.json", lines(1_000))
    base = repo.commit()
    repo.write("fixtures/demo.json", lines(1_000, {3: "x"}))
    data = repo.patch(base)
    monkeypatch.setattr(autofix, "FIX_REWRITE_BYTES_LIMIT", 1_000)
    fake = FakeGitHub(repo, base)

    outcome = open_against(fake, changes=changes_of(data))

    assert outcome.url == ""
    assert "more than the 1000 bytes" in outcome.failure
    assert not [url for method, url in fake.calls if "/git/blobs" in url]


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_blob_github_answers_wrongly_is_not_patched(encode, repo):
    """If GitHub ignored the raw media type and sent JSON, the hunks would
    fail against JSON text with a misleading reason. Checking the base blob
    first fails the attempt as GitHub's problem, not the patch's."""
    repo.write("src/cart.py", lines(20))
    base = repo.commit()
    repo.write("src/cart.py", lines(20, {2: "x"}))
    fake = FakeGitHub(repo, base)
    real_get = fake.get

    def json_instead(url, **kwargs):
        got = real_get(url, **kwargs)
        if "/git/blobs/" in url:
            got.content = b'{"content": "...", "encoding": "base64"}'
        return got

    fake.get = json_instead

    outcome = open_against(fake, changes=changes_of(repo.patch(base)))

    assert outcome == PullRequestOutcome()
    assert fake.tree_request is None


# --- The issue proxy -----------------------------------------------------------

from receiver.github_app import (  # noqa: E402 - grouped with the tests that use them
    ISSUE_SEQUENCE_BUDGET_SECONDS,
    LABEL_PAGES,
    OP_PERMISSIONS,
    FieldsOutcome,
    IssueCall,
)
from receiver.issues import OPS  # noqa: E402

ISSUE_URL = f"https://api.github.com/repos/{REPO}/issues"


def reply(status: int, body=None, *, links=None):
    """A response for the issue calls: `links` is what requests parses
    from a `Link` header, which the label listing follows."""
    mock = response(status, body)
    mock.json.return_value = body if body is not None else {}
    mock.links = links or {}
    return mock


def issue_client(*, gets=(), posts=(), patches=()) -> tuple[GitHubAppClient, MagicMock]:
    session = MagicMock()
    session.get.side_effect = list(gets)
    session.post.side_effect = list(posts)
    session.patch.side_effect = list(patches)
    return client_with(session), session


def test_every_op_has_a_downscoped_mint():
    assert set(OP_PERMISSIONS) == set(OPS)


def test_no_issue_op_mint_can_write_code_or_pull_requests():
    for permissions in OP_PERMISSIONS.values():
        assert not {"contents", "pull_requests", "workflows"} & set(permissions)
        assert not any(key.startswith("administration") for key in permissions)
        assert not any("administration" in key for key in permissions)


def test_each_op_mints_exactly_what_it_needs():
    assert OP_PERMISSIONS["file_issue"] == {"issues": "write"}
    assert OP_PERMISSIONS["comment"] == {"issues": "write"}
    assert OP_PERMISSIONS["reopen"] == {"issues": "write"}
    assert OP_PERMISSIONS["set_fields"] == {"issues": "write", ISSUE_FIELDS_PERMISSION: "read"}
    assert OP_PERMISSIONS["add_to_project"] == {
        "issues": "read", "organization_projects": "write",
    }


def test_the_issue_budget_leaves_the_lambda_time_to_settle_and_reply():
    assert ISSUE_SEQUENCE_BUDGET_SECONDS == 35


@pytest.mark.parametrize("op", OPS)
@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_mint_token_posts_the_ops_permissions(encode, op):
    session = MagicMock()
    session.get.return_value = response(200, {"id": 77})
    session.post.return_value = response(201, MINT_BODY)

    minted = client_with(session).mint_token(REPO, OP_PERMISSIONS[op])

    assert minted.token == "ghs_inst"
    assert session.post.call_args.kwargs["json"] == {
        "repositories": ["checkout"], "permissions": OP_PERMISSIONS[op],
    }


@patch("receiver.github_app.jwt.encode", return_value="app.jwt")
def test_a_mint_the_installation_refuses_returns_none(encode):
    """A permission the installation lacks is refused at the mint."""
    session = MagicMock()
    session.get.return_value = response(200, {"id": 77})
    session.post.return_value = response(422)

    assert client_with(session).mint_token(REPO, {"issues": "write"}) is None


def test_the_autofix_mint_is_the_general_mint_with_the_autofix_permissions():
    client = client_with(MagicMock())
    with patch.object(client, "mint_token", return_value="minted") as mint:
        assert client.mint_autofix_token(REPO, deadline=12.0) == "minted"

    mint.assert_called_once_with(REPO, AUTOFIX_PERMISSIONS, deadline=12.0)


def test_fetch_issue_reads_with_redirects_disabled_and_the_ops_token():
    client, session = issue_client(gets=[reply(200, {"number": 7, "state": "open"})])

    got = client.fetch_issue(REPO, 7, token="ghs_issue")

    assert got == IssueCall(ok=True, status=200, body={"number": 7, "state": "open"})
    call = session.get.call_args
    assert call.args[0] == f"{ISSUE_URL}/7"
    assert call.kwargs["allow_redirects"] is False
    assert call.kwargs["headers"]["Authorization"] == "Bearer ghs_issue"
    assert 0 < call.kwargs["timeout"] <= 15


@pytest.mark.parametrize("status", [301, 302, 404, 410, 500])
def test_a_fetch_outside_2xx_is_a_failure_naming_its_step(status):
    """A 3xx is what a transferred issue answers, and `raise_for_status`
    would have let it through."""
    client, _ = issue_client(gets=[reply(status, {"url": "elsewhere"})])

    got = client.fetch_issue(REPO, 7, token="ghs_issue")

    assert got == IssueCall(ok=False, status=status, step="read the issue")


def test_a_transport_error_is_a_failure_with_no_status():
    client, session = issue_client()
    session.get.side_effect = OSError("connection reset")

    assert client.fetch_issue(REPO, 7, token="t") == IssueCall(ok=False, step="read the issue")


def test_file_issue_sends_title_body_labels_and_type():
    client, session = issue_client(posts=[reply(201, {"number": 9, "html_url": "u"})])

    got = client.file_issue(
        REPO, token="ghs_issue", title="Fix: x", body="b", labels=["Backend"], issue_type="Bug"
    )

    assert got.ok and got.body["number"] == 9
    call = session.post.call_args
    assert call.args[0] == ISSUE_URL
    assert call.kwargs["json"] == {"title": "Fix: x", "body": "b", "labels": ["Backend"], "type": "Bug"}
    assert call.kwargs["allow_redirects"] is False


def test_file_issue_without_a_type_sends_none():
    client, session = issue_client(posts=[reply(201, {"number": 9})])

    client.file_issue(REPO, token="t", title="Fix: x", body="b")

    assert session.post.call_args.kwargs["json"] == {"title": "Fix: x", "body": "b", "labels": []}


def test_a_refused_create_reports_githubs_status():
    client, _ = issue_client(posts=[reply(422)])

    got = client.file_issue(REPO, token="t", title="Fix: x", body="b", issue_type="Nope")

    assert got == IssueCall(ok=False, status=422, step="create the issue")


def test_comment_on_issue_posts_one_comment():
    client, session = issue_client(posts=[reply(201, {"html_url": "c"})])

    got = client.comment_on_issue(REPO, 7, token="t", body="Seen again.")

    assert got.body == {"html_url": "c"}
    assert session.post.call_args.args[0] == f"{ISSUE_URL}/7/comments"
    assert session.post.call_args.kwargs["json"] == {"body": "Seen again."}


def test_reopen_comments_first_then_reopens():
    client, session = issue_client(
        posts=[reply(201, {"html_url": "c"})],
        patches=[reply(200, {"number": 7, "state": "open", "html_url": "i"})],
    )

    got = client.reopen_issue(REPO, 7, token="t", body="It came back.")

    assert got.ok and got.body["html_url"] == "i"
    assert session.post.call_args.args[0] == f"{ISSUE_URL}/7/comments"
    patch_call = session.patch.call_args
    assert patch_call.args[0] == f"{ISSUE_URL}/7"
    assert patch_call.kwargs["json"] == {"state": "open", "state_reason": "reopened"}
    assert patch_call.kwargs["allow_redirects"] is False


def test_a_failed_reopen_comment_never_reopens():
    client, session = issue_client(posts=[reply(500)])

    got = client.reopen_issue(REPO, 7, token="t", body="It came back.")

    assert got == IssueCall(ok=False, status=500, step="post the comment")
    session.patch.assert_not_called()


def test_a_reopen_that_fails_after_its_comment_names_the_reopen():
    client, _ = issue_client(posts=[reply(201, {})], patches=[reply(403)])

    got = client.reopen_issue(REPO, 7, token="t", body="It came back.")

    assert got == IssueCall(ok=False, status=403, step="reopen the issue")


def test_labels_are_listed_across_pages():
    page2 = "https://api.github.com/repositories/1/labels?per_page=100&page=2"
    client, session = issue_client(
        gets=[
            reply(200, [{"name": "Backend"}], links={"next": {"url": page2}}),
            reply(200, [{"name": "frontend"}]),
        ]
    )

    assert client.list_label_names(REPO, token="t") == ["Backend", "frontend"]
    first, second = session.get.call_args_list
    assert first.args[0] == f"https://api.github.com/repos/{REPO}/labels"
    assert first.kwargs["params"] == {"per_page": 100}
    assert second.args[0] == page2
    assert second.kwargs["params"] is None


def test_labels_stop_after_three_pages():
    pages = [
        reply(200, [{"name": f"l{i}"}], links={"next": {"url": f"https://api.github.com/x?page={i + 2}"}})
        for i in range(LABEL_PAGES)
    ]
    client, session = issue_client(gets=pages)

    assert client.list_label_names(REPO, token="t") == ["l0", "l1", "l2"]
    assert session.get.call_count == LABEL_PAGES == 3


def test_a_next_page_off_the_api_host_fails_the_listing():
    client, _ = issue_client(
        gets=[reply(200, [{"name": "a"}], links={"next": {"url": "https://evil.example/labels"}})]
    )

    assert client.list_label_names(REPO, token="t") is None


def test_a_failed_label_page_fails_the_listing():
    client, _ = issue_client(gets=[reply(502)])

    assert client.list_label_names(REPO, token="t") is None


FIELDS_DATA = {
    "data": {
        "organization": {
            "issueFields": {
                "nodes": [
                    {
                        "id": "F_pri",
                        "name": "Priority",
                        "options": [{"id": "O_high", "name": "High"}, {"id": "O_low", "name": "Low"}],
                    },
                    {
                        "id": "F_eff",
                        "name": "Effort",
                        "options": [{"id": "O_quick", "name": "Quick Win"}],
                    },
                    {},
                ]
            }
        }
    }
}


def held(*pairs) -> dict:
    return {
        "data": {
            "node": {
                "issueFieldValues": {
                    "nodes": [{"name": option, "field": {"name": name}} for name, option in pairs]
                }
            }
        }
    }


def test_set_fields_resolves_names_sets_them_in_one_call_and_reads_them_back():
    client, session = issue_client(
        posts=[
            reply(200, FIELDS_DATA),
            reply(200, {"data": {"setIssueFieldValue": {"clientMutationId": None}}}),
            reply(200, held(("Priority", "High"), ("Effort", "Quick Win"))),
        ]
    )

    got = client.set_fields(
        REPO, token="t", node_id="I_9", fields={"Priority": "high", "Effort": "Quick Win"}
    )

    assert got == FieldsOutcome(ok=True, fields={"Priority": "High", "Effort": "Quick Win"})
    discovery, mutation, readback = session.post.call_args_list
    assert discovery.args[0] == "https://api.github.com/graphql"
    assert discovery.kwargs["json"]["variables"] == {"org": "acme-tools"}
    sent = mutation.kwargs["json"]
    assert "setIssueFieldValue" in sent["query"]
    assert sent["variables"] == {
        "issue": "I_9",
        "field0": "F_pri", "option0": "O_high",
        "field1": "F_eff", "option1": "O_quick",
    }
    assert readback.kwargs["json"]["variables"] == {"id": "I_9"}
    assert all(call.kwargs["allow_redirects"] is False for call in session.post.call_args_list)
    assert "GraphQL-Features" not in discovery.kwargs["headers"]


def test_set_fields_never_carries_suggest():
    """`suggest` returns success and writes nothing, so it must never be sent."""
    client, session = issue_client(
        posts=[reply(200, FIELDS_DATA), reply(200, {"data": {}}), reply(200, held(("Priority", "High")))]
    )

    client.set_fields(REPO, token="t", node_id="I_9", fields={"Priority": "High"})

    sent = session.post.call_args_list[1].kwargs["json"]
    assert "suggest" not in sent["query"]
    assert "suggest" not in str(sent["variables"])


def test_a_value_that_did_not_persist_fails_the_op():
    client, _ = issue_client(
        posts=[reply(200, FIELDS_DATA), reply(200, {"data": {}}), reply(200, held(("Priority", "Low")))]
    )

    got = client.set_fields(REPO, token="t", node_id="I_9", fields={"Priority": "High"})

    assert got == FieldsOutcome(ok=False, step="field values did not persist")


def test_the_definitions_are_cached_for_the_container():
    client, session = issue_client(
        posts=[
            reply(200, FIELDS_DATA), reply(200, {"data": {}}), reply(200, held(("Priority", "High"))),
            reply(200, {"data": {}}), reply(200, held(("Priority", "Low"))),
        ]
    )

    client.set_fields(REPO, token="t", node_id="I_1", fields={"Priority": "High"})
    second = client.set_fields(REPO, token="t", node_id="I_2", fields={"Priority": "Low"})

    assert second.ok
    assert session.post.call_count == 5


def test_an_unknown_option_triggers_one_refetch_before_it_is_rejected():
    client, session = issue_client(posts=[reply(200, FIELDS_DATA), reply(200, FIELDS_DATA)])

    got = client.set_fields(REPO, token="t", node_id="I_9", fields={"Priority": "Someday"})

    assert got == FieldsOutcome(ok=False, rejected="unknown option for field Priority")
    assert session.post.call_count == 2


def test_an_option_added_since_the_cache_is_found_by_the_refetch():
    newer = {
        "data": {
            "organization": {
                "issueFields": {
                    "nodes": [{"id": "F_pri", "name": "Priority", "options": [{"id": "O_urgent", "name": "Urgent"}]}]
                }
            }
        }
    }
    client, session = issue_client(
        posts=[reply(200, FIELDS_DATA), reply(200, newer), reply(200, {"data": {}}), reply(200, held(("Priority", "Urgent")))]
    )

    got = client.set_fields(REPO, token="t", node_id="I_9", fields={"Priority": "Urgent"})

    assert got.ok
    assert session.post.call_args_list[2].kwargs["json"]["variables"]["option0"] == "O_urgent"


def test_a_configured_field_that_is_not_single_select_fails_the_op():
    client, _ = issue_client(posts=[reply(200, FIELDS_DATA), reply(200, FIELDS_DATA)])

    got = client.set_fields(REPO, token="t", node_id="I_9", fields={"Size": "Large"})

    assert got == FieldsOutcome(ok=False, step="field Size is not single-select")


def test_a_graphql_errors_array_is_a_failure_even_inside_a_200():
    client, _ = issue_client(posts=[reply(200, {"errors": [{"message": "Resource not accessible"}]})])

    got = client.set_fields(REPO, token="t", node_id="I_9", fields={"Priority": "High"})

    assert got == FieldsOutcome(ok=False, step="read the issue fields")


def test_add_to_project_resolves_the_board_once_and_adds_the_issue():
    client, session = issue_client(
        posts=[
            reply(200, {"data": {"organization": {"projectV2": {"id": "P_1"}}}}),
            reply(200, {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_1"}}}}),
            reply(200, {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_2"}}}}),
        ]
    )

    first = client.add_to_project(REPO, 3, token="t", node_id="I_1")
    second = client.add_to_project(REPO, 3, token="t", node_id="I_2")

    assert first == IssueCall(ok=True, status=200, body={"item_id": "PVTI_1"})
    assert second.body == {"item_id": "PVTI_2"}
    lookup, add, _ = session.post.call_args_list
    assert lookup.kwargs["json"]["variables"] == {"org": "acme-tools", "number": 3}
    assert add.kwargs["json"]["variables"] == {"project": "P_1", "content": "I_1"}


def test_a_board_that_does_not_exist_fails_the_op():
    client, _ = issue_client(posts=[reply(200, {"data": {"organization": {"projectV2": None}}})])

    got = client.add_to_project(REPO, 3, token="t", node_id="I_1")

    assert got == IssueCall(ok=False, status=200, step="read the project")


def test_a_redirect_on_a_graphql_call_is_a_failure():
    client, _ = issue_client(posts=[reply(307)])

    got = client.add_to_project(REPO, 3, token="t", node_id="I_1")

    assert got == IssueCall(ok=False, status=307, step="read the project")
