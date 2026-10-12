"""GitHub App auth, the pull requests the receiver opens for autofix, and
the issue writes it makes for the issue proxy.

Modeled on receiver.routines.RoutineClient's posture: public methods never
raise; a failure returns None or a failure object and the caller decides.
Tokens are minted per pull request or per issue operation, downscoped to
what that one needs, spent inside the invocation that minted them, and never
persisted or handed to anything outside this process.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import jwt
import requests

from receiver import autofix
from receiver.patch import FileChange, PatchError, apply_change, blob_matches, blob_sha

LOG = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 15
# App JWTs may live at most 10 minutes; 8 leaves clock-skew margin.
JWT_TTL_SECONDS = 8 * 60

# open_fix_pr issues its requests one after another (two for the mint, the
# reads, a base-blob read and a new blob per changed file, tree, commit, ref,
# pull), each with a TIMEOUT_SECONDS ceiling, inside a 60-second Lambda
# invocation. A budget on the whole sequence, not just each call, leaves the
# handler time after the sequence to settle the record and reply; each
# request gets the smaller of TIMEOUT_SECONDS and whatever remains of the
# budget. The ceiling is between bytes, not on a whole transfer, so a large
# blob that keeps moving is bounded by the budget alone.
SEQUENCE_BUDGET_SECONDS = 45

# Asks the blobs endpoint for the bytes themselves rather than base64 inside
# JSON, which would cost a third more transfer and a decode for every base
# file a patch modifies.
RAW_BLOB = "application/vnd.github.raw+json"

# The whole autofix permission grant. `workflows` is deliberately absent: a
# workflow-file change pushed to a branch runs in CI, with access to the
# repository's secrets, before any human reviews the PR. The payload check
# in receiver.autofix rejects such paths before any call is made, and this
# scope is the enforcement behind it: a token minted from this dict cannot
# push one.
AUTOFIX_PERMISSIONS = {"contents": "write", "pull_requests": "write"}

# The installation's permission key for the organization's Issue fields,
# read-only. GitHub's App settings page names the permission but not its
# key, and the installation-token reference does not list it, so the key was
# read back from the approved installation (`GET /app/installations/{id}`)
# and proved by minting a token that carries it. `set_fields` needs it to
# resolve field and option names; no other mint carries it.
ISSUE_FIELDS_PERMISSION = "issue_fields"

# Each issue op's mint, downscoped to what that op needs, the way
# AUTOFIX_PERMISSIONS scopes a fix. No entry carries `contents`,
# `pull_requests`, `workflows` or any administration permission, so an
# issue op's token cannot write a file or open a pull request whatever its
# request said.
OP_PERMISSIONS = {
    "file_issue": {"issues": "write"},
    "comment": {"issues": "write"},
    "reopen": {"issues": "write"},
    "set_fields": {"issues": "write", ISSUE_FIELDS_PERMISSION: "read"},
    "add_to_project": {"issues": "read", "organization_projects": "write"},
}

# The GitHub part of one issue op, from the mint to its last call. The
# longest op is six calls (the two-call mint, three label pages, the
# create), each capped at TIMEOUT_SECONDS; what is left of the Lambda's 60
# seconds covers the settle writes and the Teams reply.
ISSUE_SEQUENCE_BUDGET_SECONDS = 35

GRAPHQL_URL = f"{API_BASE}/graphql"

# A repository's labels are listed 100 a page for at most this many pages.
# A requested label past the last page reads as unknown and is dropped
# loudly, rather than spending the op's budget on more pages.
LABEL_PAGES = 3

# The organization's single-select issue fields and their options, the same
# discovery the target repository's own issue tooling runs. A field of any
# other type comes back as an empty node.
ISSUE_FIELDS_QUERY = """
query($org: String!) {
  organization(login: $org) {
    issueFields(first: 50) {
      nodes { ... on IssueFieldSingleSelect { id name options { id name } } }
    }
  }
}
"""

# What an issue's single-select fields hold, read back after a write:
# `setIssueFieldValue` has a known mode that reports success and writes
# nothing.
FIELD_VALUES_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on Issue {
      issueFieldValues(first: 20) {
        nodes {
          ... on IssueFieldSingleSelectValue {
            name
            field { ... on IssueFieldSingleSelect { name } }
          }
        }
      }
    }
  }
}
"""

PROJECT_QUERY = """
query($org: String!, $number: Int!) {
  organization(login: $org) { projectV2(number: $number) { id } }
}
"""

# Adding an issue that is already on the board returns the existing item, so
# the call is idempotent at GitHub as well as in the receiver.
ADD_PROJECT_ITEM_MUTATION = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
}
"""

# The mode a whole file gets when the base tree does not already carry the
# path. A path the base carries as an executable keeps its executable bit;
# one it carries as anything but a regular file is refused outright.
BLOB_MODE = "100644"


@dataclass(frozen=True)
class PullRequestOutcome:
    """What open_fix_pr did. `url` is set when a pull request opened.
    Otherwise `failure` says why when the fix itself was the problem: a
    patch that does not apply at the base commit, or a path the base tree
    carries as a symlink. It is empty when the problem was GitHub's, which
    the log line names instead."""

    url: str = ""
    failure: str = ""


@dataclass(frozen=True)
class MintedToken:
    """One scoped installation token and when GitHub will kill it."""

    token: str
    expires_at: str  # ISO-8601, straight from the GitHub response


@dataclass(frozen=True)
class IssueCall:
    """What one issue-proxy call did. On a 2xx, `ok` is True and `body` is
    GitHub's JSON. Otherwise `status` is GitHub's answer (0 when none came
    back) and `step` names the call that failed, which the handler returns
    as the reason for its 502."""

    ok: bool
    status: int = 0
    body: dict = field(default_factory=dict)
    step: str = ""


@dataclass(frozen=True)
class FieldsOutcome:
    """What `set_fields` did. `fields` is each field name mapped to the
    option name GitHub holds, on success. `rejected` says why the request
    itself was wrong (an option no field offers), which the handler answers
    with a 400; `step` says which call failed, answered with a 502."""

    ok: bool
    fields: dict = field(default_factory=dict)
    rejected: str = ""
    step: str = ""


class GitHubCallFailed(RuntimeError):
    """One issue-proxy call answered outside 200 to 299, or not at all."""

    def __init__(self, step: str, status: int, detail: str = ""):
        super().__init__(f"{step}: HTTP {status} {detail}".strip())
        self.step = step
        self.status = status


class GitHubAppClient:
    """Mints installation tokens scoped to one repository and opens the pull
    requests that carry autofix changes."""

    def __init__(self, app_id: str, private_key_pem: str):
        self.app_id = app_id
        self.private_key_pem = private_key_pem
        self.session = requests.Session()
        # Per container: an organization's issue-field definitions, by field
        # name, and project node ids by (organization, number). Both change
        # rarely. A missing name triggers one refetch and a refused field write
        # drops the organization's entry, so a stale one costs at most one failed
        # call, never a wrong write.
        self._issue_fields: dict[str, dict[str, dict]] = {}
        self._project_ids: dict[tuple[str, int], str] = {}

    def _app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + JWT_TTL_SECONDS, "iss": self.app_id},
            self.private_key_pem,
            algorithm="RS256",
        )

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": ACCEPT,
            "X-GitHub-Api-Version": API_VERSION,
        }

    @staticmethod
    def _timeout(deadline: float | None) -> float:
        """The timeout for one request: TIMEOUT_SECONDS, or whatever is
        left of `deadline` if that is smaller. Raises once the budget is
        gone, so a sequence stops before sending a request that has no
        time left to answer."""
        if deadline is None:
            return TIMEOUT_SECONDS
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("GitHub sequence budget exhausted")
        return min(TIMEOUT_SECONDS, remaining)

    def _installation_token(
        self, repo: str, permissions: dict[str, str], *, deadline: float | None = None
    ) -> dict:
        """Mint a token for `repo`'s installation, downscoped to exactly
        `permissions` and to that one repository. Raises on failure.

        Both calls refuse redirects and treat any answer outside 200 to 299
        as failure, as every issue-proxy call does: `raise_for_status` alone
        would let a 3xx through."""
        app_jwt = self._app_jwt()
        lookup = self.session.get(
            f"{API_BASE}/repos/{repo}/installation",
            headers=self._headers(app_jwt),
            timeout=self._timeout(deadline),
            allow_redirects=False,
        )
        if not 200 <= lookup.status_code < 300:
            raise GitHubCallFailed("look up the installation", lookup.status_code)
        minted = self.session.post(
            f"{API_BASE}/app/installations/{lookup.json()['id']}/access_tokens",
            headers=self._headers(app_jwt),
            json={
                "repositories": [repo.split("/", 1)[1]],
                "permissions": permissions,
            },
            timeout=self._timeout(deadline),
            allow_redirects=False,
        )
        if not 200 <= minted.status_code < 300:
            raise GitHubCallFailed("mint the token", minted.status_code)
        return minted.json()

    def mint_token(
        self, repo: str, permissions: dict[str, str], *, deadline: float | None = None
    ) -> MintedToken | None:
        """A one-hour token scoped to `repo` and downscoped to `permissions`.

        None on any failure, including a permission the installation does
        not hold. Never raises: every caller sits in a request path, where an
        exception would leave a claimed record unsettled.
        """
        try:
            body = self._installation_token(repo, permissions, deadline=deadline)
            return MintedToken(
                token=body["token"], expires_at=str(body.get("expires_at") or "")
            )
        except Exception as exc:  # noqa: BLE001 - auth/transport must classify, not crash
            LOG.error(
                "token mint failed for %s (%s): %s", repo, ", ".join(sorted(permissions)), exc
            )
            return None

    def mint_autofix_token(
        self, repo: str, *, deadline: float | None = None
    ) -> MintedToken | None:
        """A one-hour token scoped to `repo` with AUTOFIX_PERMISSIONS."""
        return self.mint_token(repo, AUTOFIX_PERMISSIONS, deadline=deadline)

    def _call(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        *,
        deadline: float | None = None,
        **kwargs,
    ) -> dict:
        """One GitHub call, raising on any non-2xx so a sequence stops at
        its first failure and the guard around it reports that one."""
        got = getattr(self.session, method)(
            url, headers=headers, timeout=self._timeout(deadline), **kwargs
        )
        got.raise_for_status()
        return got.json()

    def open_fix_pr(
        self,
        *,
        repo: str,
        base_sha: str,
        base_branch: str,
        branch: str,
        title: str,
        body: str,
        commit_message: str,
        files: Sequence[tuple[str, str]] = (),
        changes: Sequence[FileChange] = (),
    ) -> PullRequestOutcome:
        """Create `branch` at `base_sha` carrying the fix in one commit
        described by `commit_message`, open a pull request against
        `base_branch`, and say what happened. Never raises.

        The fix is `files`, whole contents to write, or `changes`, a parsed
        patch to apply; the payload validator lets exactly one through.
        Everything runs against the Git Data API and none of it against a
        working tree, bounded by SEQUENCE_BUDGET_SECONDS. The ref read
        confirms the pull request's base exists; the compare read confirms
        `base_sha` is actually on `base_branch` (identical to it or an
        ancestor of it), since a fix built on a commit that has since
        diverged would carry unrelated changes into the pull request. The
        base tree's entries for the paths the fix touches are then checked
        before anything is written: no fix replaces a symlink, a submodule,
        or a directory, or writes through one. The new tree is created with
        `base_tree`, so it inherits every entry of the base commit and
        overrides only the fix's paths: a three-file fix costs three blob
        calls whatever the repository's size. A whole file keeps the
        executable bit the base file had. Nothing here sets a commit
        author, so GitHub records the App installation, which is what makes
        the pull request show the App as its author. A failure after the
        ref call leaves that branch behind; the runbook says how to find
        one, and a retry is a re-fire with a fresh dispatch id and
        therefore a fresh branch name.
        """
        deadline = time.monotonic() + SEQUENCE_BUDGET_SECONDS
        minted = self.mint_autofix_token(repo, deadline=deadline)
        if minted is None:
            return PullRequestOutcome()
        headers = self._headers(minted.token)
        base = f"{API_BASE}/repos/{repo}"
        try:
            # Confirms the pull request's base exists; its SHA is not used,
            # because the fix was written against `base_sha`, not the tip.
            self._call(
                "get", f"{base}/git/ref/heads/{base_branch}", headers, deadline=deadline
            )
            compare = self._call(
                "get",
                f"{base}/compare/{base_branch}...{base_sha}",
                headers,
                deadline=deadline,
            )
            if compare.get("status") not in ("identical", "behind"):
                LOG.error(
                    "autofix base commit %s is not on %s for %s", base_sha, base_branch, repo
                )
                return PullRequestOutcome()
            parent = self._call(
                "get", f"{base}/git/commits/{base_sha}", headers, deadline=deadline
            )
            root = parent["tree"]["sha"]
            paths = [path for path, _ in files] + [p for c in changes for p in c.paths]
            entries = self._base_entries(base, root, paths, headers, deadline=deadline)
            for path in paths:
                problem = _base_problem(path, entries)
                if problem:
                    LOG.error("autofix fix for %s refused: %s", repo, problem)
                    return PullRequestOutcome(failure=problem)
            if changes:
                tree_entries = self._patched_entries(
                    base, changes, entries, headers, deadline=deadline
                )
            else:
                tree_entries = self._whole_file_entries(
                    base, files, entries, headers, deadline=deadline
                )
            new_tree = self._call(
                "post", f"{base}/git/trees", headers,
                deadline=deadline,
                json={"base_tree": root, "tree": tree_entries},
            )
            commit = self._call(
                "post", f"{base}/git/commits", headers,
                deadline=deadline,
                json={
                    "message": commit_message,
                    "tree": new_tree["sha"],
                    "parents": [base_sha],
                },
            )
            self._call(
                "post", f"{base}/git/refs", headers,
                deadline=deadline,
                json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )
            pull = self._call(
                "post", f"{base}/pulls", headers,
                deadline=deadline,
                json={
                    "title": title,
                    "body": body,
                    "head": branch,
                    "base": base_branch,
                    "draft": False,
                },
            )
            url = str(pull.get("html_url") or "")
        except PatchError as exc:
            LOG.error("autofix patch for %s refused: %s", repo, exc)
            return PullRequestOutcome(failure=str(exc))
        except Exception as exc:  # noqa: BLE001 - the handler settles the record, not a traceback
            LOG.error("autofix pull request not opened for %s: %s", repo, exc)
            return PullRequestOutcome()
        if not url:
            LOG.error("autofix pull request for %s came back without a URL", repo)
            return PullRequestOutcome()
        return PullRequestOutcome(url=url)

    def _base_entries(
        self,
        base: str,
        root: str,
        paths: Sequence[str],
        headers: dict[str, str],
        *,
        deadline: float,
    ) -> dict[str, dict]:
        """The base tree's entries, keyed by path, for every path the fix
        touches and every directory above one. The recursive listing
        answers in one call. Past GitHub's listing limit it comes back
        truncated, and then only the directories on the fix's own paths
        are read, one level at a time."""
        listing = self._call(
            "get", f"{base}/git/trees/{root}", headers,
            deadline=deadline, params={"recursive": "1"},
        )
        if not listing.get("truncated"):
            return {entry["path"]: entry for entry in listing["tree"]}
        LOG.warning("base tree listing truncated; reading the fix's directories one at a time")
        entries: dict[str, dict] = {}
        levels: dict[str, list[dict]] = {}
        for path in paths:
            sha, parts = root, path.split("/")
            for depth, name in enumerate(parts):
                if sha not in levels:
                    level = self._call(
                        "get", f"{base}/git/trees/{sha}", headers, deadline=deadline
                    )
                    if level.get("truncated"):
                        raise RuntimeError("a directory on the fix's path is too large to list")
                    levels[sha] = level["tree"]
                entry = next((e for e in levels[sha] if e["path"] == name), None)
                if entry is None:
                    break
                prefix = "/".join(parts[: depth + 1])
                entries[prefix] = {**entry, "path": prefix}
                if entry.get("type") != "tree":
                    break
                sha = entry["sha"]
        return entries

    def _whole_file_entries(
        self,
        base: str,
        files: Sequence[tuple[str, str]],
        entries: dict[str, dict],
        headers: dict[str, str],
        *,
        deadline: float,
    ) -> list[dict]:
        tree = []
        for path, content in files:
            blob = self._call(
                "post", f"{base}/git/blobs", headers,
                deadline=deadline,
                json={"content": content, "encoding": "utf-8"},
            )
            mode = "100755" if entries.get(path, {}).get("mode") == "100755" else BLOB_MODE
            tree.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
        return tree

    def _patched_entries(
        self,
        base: str,
        changes: Sequence[FileChange],
        entries: dict[str, dict],
        headers: dict[str, str],
        *,
        deadline: float,
    ) -> list[dict]:
        """The tree entries a patch produces. Every change is checked
        against the base tree before any content moves, so a patch made
        against some other commit fails before it costs a transfer."""
        rewrite = 0
        for change in changes:
            old = entries.get(change.old_path) if change.old_path else None
            if change.old_path and old is None:
                raise PatchError(f"{change.old_path} does not exist at base_sha")
            if old and change.old_blob and not blob_matches(old["sha"], change.old_blob):
                raise PatchError(
                    f"{change.old_path} differs at base_sha from the version the patch "
                    "was made against"
                )
            if old and change.old_mode and change.old_mode != old["mode"]:
                raise PatchError(
                    f"{change.old_path} has mode {old['mode']} at base_sha, "
                    f"not {change.old_mode}"
                )
            if change.new_path and change.new_path != change.old_path and change.new_path in entries:
                raise PatchError(f"{change.new_path} already exists at base_sha")
            if old and change.rewrites:
                rewrite += int(old.get("size") or 0)
        limit = autofix.FIX_REWRITE_BYTES_LIMIT
        if rewrite > limit:
            raise PatchError(
                f"the files the patch modifies come to {rewrite} bytes at base_sha, "
                f"more than the {limit} bytes the receiver rewrites"
            )

        tree = []
        for change in changes:
            old = entries.get(change.old_path) if change.old_path else None
            if change.new_path is None:
                tree.append(_deletion(change.old_path, old))
                continue
            if change.rewrites:
                content = apply_change(
                    change,
                    self._blob(base, old["sha"], headers, deadline=deadline) if old else b"",
                )
                sha = self._call(
                    "post", f"{base}/git/blobs", headers,
                    deadline=deadline,
                    json={"content": base64.b64encode(content).decode("ascii"),
                          "encoding": "base64"},
                )["sha"]
            else:
                sha = old["sha"]
            mode = change.new_mode or old["mode"]
            tree.append({"path": change.new_path, "mode": mode, "type": "blob", "sha": sha})
            if change.old_path and change.old_path != change.new_path:
                tree.append(_deletion(change.old_path, old))
        return tree

    def _blob(
        self, base: str, sha: str, headers: dict[str, str], *, deadline: float
    ) -> bytes:
        """One base blob's bytes, checked against its id. A mismatch means
        GitHub answered with something other than the raw content, which is
        not the patch's fault and must not read as though it were."""
        got = self.session.get(
            f"{base}/git/blobs/{sha}",
            headers={**headers, "Accept": RAW_BLOB},
            timeout=self._timeout(deadline),
        )
        got.raise_for_status()
        content = got.content
        if blob_sha(content) != sha:
            raise RuntimeError(f"blob {sha} came back as other content")
        return content

    # ---- Issue proxy ----
    #
    # The calls behind the five issue ops. Every one takes its repository
    # from the caller, which passes the configured target repository and
    # never anything from a request; passes `allow_redirects=False`, because
    # `requests` follows a GET's redirect by default and a transferred issue
    # answers with one into another repository; and treats any status
    # outside 200 to 299 as failure, where `_call`'s `raise_for_status`
    # would let a 3xx through. Each takes the token its op minted.

    def _strict(
        self, method: str, url: str, token: str, *, step: str, deadline: float | None, **kwargs
    ) -> requests.Response:
        """One issue-proxy call. Raises GitHubCallFailed on any answer outside
        200 to 299, a transport error, or a spent budget."""
        try:
            got = getattr(self.session, method)(
                url,
                headers=self._headers(token),
                timeout=self._timeout(deadline),
                allow_redirects=False,
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - transport and budget failures classify, never crash
            raise GitHubCallFailed(step, 0, str(exc)) from exc
        if not 200 <= got.status_code < 300:
            raise GitHubCallFailed(step, got.status_code)
        return got

    def _graphql(
        self, query: str, variables: dict, token: str, *, step: str, deadline: float | None
    ) -> dict:
        """One GraphQL call; returns its `data`. GraphQL reports errors
        inside a 200, so a non-empty `errors` array is a failure too. No
        `GraphQL-Features` header is sent: the target repository's own
        tooling runs these queries without one."""
        got = self._strict(
            "post", GRAPHQL_URL, token,
            step=step, deadline=deadline, json={"query": query, "variables": variables},
        )
        body = got.json()
        if body.get("errors"):
            raise GitHubCallFailed(step, got.status_code, f"GraphQL errors: {str(body['errors'])[:500]}")
        return body.get("data") or {}

    def _issue_call(
        self, method: str, url: str, token: str, *, step: str, deadline: float | None, **kwargs
    ) -> IssueCall:
        """`_strict` with the never-raise posture: the outcome as an IssueCall."""
        try:
            got = self._strict(method, url, token, step=step, deadline=deadline, **kwargs)
            return IssueCall(ok=True, status=got.status_code, body=got.json())
        except GitHubCallFailed as exc:
            LOG.error("issue call failed: %s", exc)
            return IssueCall(ok=False, status=exc.status, step=exc.step)
        except Exception as exc:  # noqa: BLE001 - an unreadable body classifies, never crashes
            LOG.error("issue call %s returned an unreadable body: %s", step, exc)
            return IssueCall(ok=False, step=step)

    def fetch_issue(
        self, repo: str, number: int, *, token: str, deadline: float | None = None
    ) -> IssueCall:
        """Read issue `number` in `repo`, redirects disabled, so the handler
        can check it is an issue in that repository before writing to it."""
        return self._issue_call(
            "get", f"{API_BASE}/repos/{repo}/issues/{number}", token,
            step="read the issue", deadline=deadline,
        )

    def list_label_names(
        self, repo: str, *, token: str, deadline: float | None = None
    ) -> list[str] | None:
        """The repository's label names, following `Link: rel="next"` for
        at most LABEL_PAGES pages of 100. None on any failure."""
        url = f"{API_BASE}/repos/{repo}/labels"
        params: dict | None = {"per_page": 100}
        names: list[str] = []
        try:
            for _ in range(LABEL_PAGES):
                got = self._strict(
                    "get", url, token, step="list the labels", deadline=deadline, params=params
                )
                names += [str(label.get("name") or "") for label in got.json()]
                url = str((got.links.get("next") or {}).get("url") or "")
                params = None  # the next link carries its own query
                if not url:
                    break
                if not url.startswith(f"{API_BASE}/"):
                    raise GitHubCallFailed("list the labels", got.status_code, "next page off the API host")
            else:
                if url:
                    LOG.warning(
                        "%s has more than %d pages of labels; later ones read as unknown",
                        repo, LABEL_PAGES,
                    )
        except Exception as exc:  # noqa: BLE001 - the handler answers 502, never a traceback
            LOG.error("label list failed for %s: %s", repo, exc)
            return None
        return [name for name in names if name]

    def file_issue(
        self,
        repo: str,
        *,
        token: str,
        title: str,
        body: str,
        labels: Sequence[str] = (),
        issue_type: str = "",
        deadline: float | None = None,
    ) -> IssueCall:
        """Create an issue. `type` is the issue type's name, which the
        create-issue reference accepts with the Issues permission alone."""
        payload: dict = {"title": title, "body": body, "labels": list(labels)}
        if issue_type:
            payload["type"] = issue_type
        return self._issue_call(
            "post", f"{API_BASE}/repos/{repo}/issues", token,
            step="create the issue", deadline=deadline, json=payload,
        )

    def comment_on_issue(
        self, repo: str, number: int, *, token: str, body: str, deadline: float | None = None
    ) -> IssueCall:
        """Comment on issue `number`; `body` is GitHub's comment, with its
        own `html_url`."""
        return self._issue_call(
            "post", f"{API_BASE}/repos/{repo}/issues/{number}/comments", token,
            step="post the comment", deadline=deadline, json={"body": body},
        )

    def reopen_issue(
        self, repo: str, number: int, *, token: str, body: str, deadline: float | None = None
    ) -> IssueCall:
        """Comment on closed issue `number`, then reopen it. The comment goes
        first so a reopened issue never lacks its explanation; a failed
        reopen after it names its own step, and a retry runs both again,
        since a second comment is a smaller harm than a closed issue."""
        comment = self.comment_on_issue(repo, number, token=token, body=body, deadline=deadline)
        if not comment.ok:
            return comment
        return self._issue_call(
            "patch", f"{API_BASE}/repos/{repo}/issues/{number}", token,
            step="reopen the issue", deadline=deadline,
            json={"state": "open", "state_reason": "reopened"},
        )

    def _field_definitions(
        self, org: str, token: str, *, deadline: float | None, refresh: bool = False
    ) -> dict[str, dict]:
        """The organization's single-select issue fields by name, each with
        its id and its options by lowercased name. Cached per container."""
        if refresh or org not in self._issue_fields:
            data = self._graphql(
                ISSUE_FIELDS_QUERY, {"org": org}, token,
                step="read the issue fields", deadline=deadline,
            )
            nodes = ((data.get("organization") or {}).get("issueFields") or {}).get("nodes") or []
            self._issue_fields[org] = {
                node["name"]: {
                    "id": node["id"],
                    "options": {
                        str(option["name"]).lower(): (option["name"], option["id"])
                        for option in node.get("options") or []
                    },
                }
                for node in nodes
                if node.get("id") and node.get("name")
            }
        return self._issue_fields[org]

    def set_fields(
        self,
        repo: str,
        *,
        token: str,
        node_id: str,
        fields: dict[str, str],
        deadline: float | None = None,
    ) -> FieldsOutcome:
        """Set single-select issue fields on the issue `node_id` names, by
        field and option name, in one `setIssueFieldValue` call, then read
        the values back. The organization is `repo`'s owner.

        An option name matches ignoring case and is sent as GitHub's own.
        A name missing from the cached definitions triggers one refetch;
        an option still unknown after it is the request's fault (rejected),
        and a field still unknown is a configured field that is not
        single-select, which fails the op. `suggest` is never passed: the
        mutation returns success with it and writes nothing.
        """
        org = repo.split("/", 1)[0]
        try:
            definitions = self._field_definitions(org, token, deadline=deadline)
            resolved, unknown_field, unknown_option = _resolve_fields(definitions, fields)
            if unknown_field or unknown_option:
                definitions = self._field_definitions(org, token, deadline=deadline, refresh=True)
                resolved, unknown_field, unknown_option = _resolve_fields(definitions, fields)
            if unknown_field:
                LOG.error("issue field %s is not a single-select field in %s", unknown_field, org)
                return FieldsOutcome(ok=False, step=f"field {unknown_field} is not single-select")
            if unknown_option:
                return FieldsOutcome(ok=False, rejected=f"unknown option for field {unknown_option}")

            declared = ["$issue: ID!"]
            entries = []
            variables: dict = {"issue": node_id}
            for index, (_, field_id, _, option_id) in enumerate(resolved):
                declared += [f"$field{index}: ID!", f"$option{index}: ID!"]
                entries.append(f"{{fieldId: $field{index}, singleSelectOptionId: $option{index}}}")
                variables[f"field{index}"] = field_id
                variables[f"option{index}"] = option_id
            mutation = (
                f"mutation({', '.join(declared)}) {{ setIssueFieldValue(input: "
                f"{{issueId: $issue, issueFields: [{', '.join(entries)}]}}) "
                "{ clientMutationId } }"
            )
            try:
                self._graphql(
                    mutation, variables, token, step="set the field values", deadline=deadline
                )
            except GitHubCallFailed:
                # An option deleted and recreated under the same name keeps
                # the name but not the id, which no name lookup notices. Drop
                # the cached definitions so the session's retry reads them
                # again rather than failing until the container recycles.
                self._issue_fields.pop(org, None)
                raise

            data = self._graphql(
                FIELD_VALUES_QUERY, {"id": node_id}, token,
                step="read the field values back", deadline=deadline,
            )
            values = ((data.get("node") or {}).get("issueFieldValues") or {}).get("nodes") or []
            held = {
                str((value.get("field") or {}).get("name") or ""): value.get("name")
                for value in values
            }
            expected = {name: option for name, _, option, _ in resolved}
            missed = sorted(name for name, option in expected.items() if held.get(name) != option)
            if missed:
                LOG.error("field values did not persist on %s: %s", node_id, ", ".join(missed))
                return FieldsOutcome(ok=False, step="field values did not persist")
            return FieldsOutcome(ok=True, fields=expected)
        except GitHubCallFailed as exc:
            LOG.error("set_fields failed for %s: %s", repo, exc)
            return FieldsOutcome(ok=False, step=exc.step)
        except Exception as exc:  # noqa: BLE001 - the handler answers 502, never a traceback
            LOG.error("set_fields failed for %s: %s", repo, exc)
            return FieldsOutcome(ok=False, step="set the field values")

    def add_to_project(
        self,
        repo: str,
        project_number: int,
        *,
        token: str,
        node_id: str,
        deadline: float | None = None,
    ) -> IssueCall:
        """Add the issue `node_id` names to organization project
        `project_number`, the organization being `repo`'s owner."""
        org = repo.split("/", 1)[0]
        key = (org, project_number)
        try:
            if key not in self._project_ids:
                data = self._graphql(
                    PROJECT_QUERY, {"org": org, "number": project_number}, token,
                    step="read the project", deadline=deadline,
                )
                project_id = ((data.get("organization") or {}).get("projectV2") or {}).get("id")
                if not project_id:
                    raise GitHubCallFailed("read the project", 200, "no such project")
                self._project_ids[key] = project_id
            data = self._graphql(
                ADD_PROJECT_ITEM_MUTATION,
                {"project": self._project_ids[key], "content": node_id},
                token, step="add the issue to the project", deadline=deadline,
            )
            item = ((data.get("addProjectV2ItemById") or {}).get("item") or {}).get("id")
            if not item:
                raise GitHubCallFailed("add the issue to the project", 200, "no item came back")
            return IssueCall(ok=True, status=200, body={"item_id": item})
        except GitHubCallFailed as exc:
            LOG.error("add_to_project failed for %s: %s", repo, exc)
            return IssueCall(ok=False, status=exc.status, step=exc.step)
        except Exception as exc:  # noqa: BLE001 - the handler answers 502, never a traceback
            LOG.error("add_to_project failed for %s: %s", repo, exc)
            return IssueCall(ok=False, step="add the issue to the project")


def _resolve_fields(
    definitions: dict[str, dict], fields: dict[str, str]
) -> tuple[list[tuple[str, str, str, str]], str, str]:
    """Each requested field and option as (field name, field id, option name
    as GitHub spells it, option id), plus the first field and the first
    option that could not be resolved ("" when none)."""
    resolved = []
    for name, option in fields.items():
        definition = definitions.get(name)
        if definition is None:
            return [], name, ""
        match = definition["options"].get(option.lower())
        if match is None:
            return [], "", name
        resolved.append((name, definition["id"], match[0], match[1]))
    return resolved, "", ""


def _deletion(path: str, entry: dict) -> dict:
    """A tree entry that removes `path`: the Git Data API deletes a path
    given a null sha against `base_tree`."""
    return {"path": path, "mode": entry["mode"], "type": "blob", "sha": None}


def _base_problem(path: str, entries: dict[str, dict]) -> str:
    """Why the fix may not touch `path` given the base tree, or "". Shared
    by both payload forms: whole files declare no mode, and a pure rename
    in a patch declares none either, so only the base tree can say what a
    path already is."""
    entry = entries.get(path)
    if entry is not None and entry.get("mode") not in autofix.REGULAR_FILE_MODES:
        return f"{path} is {autofix.describe_mode(entry.get('mode', ''))} at base_sha"
    parts = path.split("/")
    for depth in range(1, len(parts)):
        above = "/".join(parts[:depth])
        parent = entries.get(above)
        if parent is not None and parent.get("type") != "tree":
            return f"{path} runs through {above}, which is not a directory at base_sha"
    return ""
