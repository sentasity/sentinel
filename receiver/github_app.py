"""GitHub App auth and the pull requests the receiver opens for autofix.

Modeled on receiver.routines.RoutineClient's posture: public methods never
raise; a failure returns None and the caller decides. Tokens are minted per
pull request, spent inside the invocation that minted them, and never
persisted or handed to anything outside this process.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import jwt
import requests

LOG = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 15
# App JWTs may live at most 10 minutes; 8 leaves clock-skew margin.
JWT_TTL_SECONDS = 8 * 60

# open_fix_pr can issue up to 28 sequential requests (two for the mint, the
# reads, one blob per file up to twenty, tree, commit, ref, pull), each with
# a TIMEOUT_SECONDS ceiling, inside a 60-second Lambda invocation. A budget
# on the whole sequence, not just each call, leaves the handler time after
# the sequence to settle the record and reply; each request gets the
# smaller of TIMEOUT_SECONDS and whatever remains of the budget.
SEQUENCE_BUDGET_SECONDS = 45

# The whole autofix permission grant. `workflows` is deliberately absent: a
# workflow-file change pushed to a branch runs in CI, with access to the
# repository's secrets, before any human reviews the PR. The payload check
# in receiver.autofix rejects such paths before any call is made, and this
# scope is the enforcement behind it: a token minted from this dict cannot
# push one.
AUTOFIX_PERMISSIONS = {"contents": "write", "pull_requests": "write"}

# The mode a new blob gets when the base tree does not already carry the
# path, or carries it as something other than a regular executable file: a
# symlink or submodule entry at a submitted path is not preserved either,
# the fix writes a regular file there, which a reviewer sees in the diff.
BLOB_MODE = "100644"


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
        files: list[tuple[str, str]],
        title: str,
        body: str,
    ) -> str | None:
        """Create `branch` at `base_sha` carrying `files`, open a pull
        request against `base_branch`, and return its URL. None on any
        failure; never raises.

        Up to eight calls against the Git Data API and none against a
        working tree, bounded by SEQUENCE_BUDGET_SECONDS. The ref read
        confirms the pull request's base exists; the compare read confirms
        `base_sha` is actually on `base_branch` (identical to it or an
        ancestor of it), since a fix built on a commit that has since
        diverged would carry unrelated changes into the pull request. The
        tree is created with `base_tree`, so it inherits every entry of
        the base commit and overrides only the listed paths: a three-file
        fix costs three blob calls whatever the repository's size. The
        base tree listing lets each new blob keep the executable bit an
        existing file already had. Nothing here sets a commit author, so
        GitHub records the App installation, which is what makes the pull
        request show the App as its author. A failure after the ref call
        leaves that branch behind; the runbook says how to find one, and a
        retry is a re-fire with a fresh dispatch id and therefore a fresh
        branch name.
        """
        deadline = time.monotonic() + SEQUENCE_BUDGET_SECONDS
        minted = self.mint_autofix_token(repo, deadline=deadline)
        if minted is None:
            return None
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
                return None
            parent = self._call(
                "get", f"{base}/git/commits/{base_sha}", headers, deadline=deadline
            )
            tree = self._call(
                "get",
                f"{base}/git/trees/{parent['tree']['sha']}",
                headers,
                deadline=deadline,
                params={"recursive": "1"},
            )
            if tree.get("truncated"):
                LOG.warning(
                    "base tree listing truncated for %s; new blobs default to regular files",
                    repo,
                )
                modes: dict[str, str] = {}
            else:
                modes = {
                    entry["path"]: entry["mode"]
                    for entry in tree["tree"]
                    if entry.get("type") == "blob"
                }
            entries = []
            for path, content in files:
                blob = self._call(
                    "post", f"{base}/git/blobs", headers,
                    deadline=deadline,
                    json={"content": content, "encoding": "utf-8"},
                )
                mode = "100755" if modes.get(path) == "100755" else BLOB_MODE
                entries.append(
                    {"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]}
                )
            new_tree = self._call(
                "post", f"{base}/git/trees", headers,
                deadline=deadline,
                json={"base_tree": parent["tree"]["sha"], "tree": entries},
            )
            commit = self._call(
                "post", f"{base}/git/commits", headers,
                deadline=deadline,
                json={"message": title, "tree": new_tree["sha"], "parents": [base_sha]},
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
        except Exception as exc:  # noqa: BLE001 - the handler settles the record, not a traceback
            LOG.error("autofix pull request not opened for %s: %s", repo, exc)
            return None
        if not url:
            LOG.error("autofix pull request for %s came back without a URL", repo)
            return None
        return url
