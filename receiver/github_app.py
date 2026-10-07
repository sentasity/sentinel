"""GitHub App auth and the pull requests the receiver opens for autofix.

Modeled on receiver.routines.RoutineClient's posture: public methods never
raise; a failure returns None and the caller decides. Tokens are minted per
pull request, spent inside the invocation that minted them, and never
persisted or handed to anything outside this process.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

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


class GitHubAppClient:
    """Mints installation tokens scoped to one repository and opens the pull
    requests that carry autofix changes."""

    def __init__(self, app_id: str, private_key_pem: str):
        self.app_id = app_id
        self.private_key_pem = private_key_pem
        self.session = requests.Session()

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
        `permissions` and to that one repository. Raises on failure."""
        app_jwt = self._app_jwt()
        lookup = self.session.get(
            f"{API_BASE}/repos/{repo}/installation",
            headers=self._headers(app_jwt),
            timeout=self._timeout(deadline),
        )
        lookup.raise_for_status()
        minted = self.session.post(
            f"{API_BASE}/app/installations/{lookup.json()['id']}/access_tokens",
            headers=self._headers(app_jwt),
            json={
                "repositories": [repo.split("/", 1)[1]],
                "permissions": permissions,
            },
            timeout=self._timeout(deadline),
        )
        minted.raise_for_status()
        return minted.json()

    def mint_autofix_token(
        self, repo: str, *, deadline: float | None = None
    ) -> MintedToken | None:
        """A one-hour token scoped to `repo` with AUTOFIX_PERMISSIONS.

        None on any failure. Never raises: the caller sits in the callback
        request path, where an exception would leave a claimed record
        unsettled.
        """
        try:
            body = self._installation_token(repo, AUTOFIX_PERMISSIONS, deadline=deadline)
            return MintedToken(
                token=body["token"], expires_at=str(body.get("expires_at") or "")
            )
        except Exception as exc:  # noqa: BLE001 - auth/transport must classify, not crash
            LOG.error("autofix token mint failed for %s: %s", repo, exc)
            return None

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
