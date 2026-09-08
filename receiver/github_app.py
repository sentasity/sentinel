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

# The whole autofix permission grant. `workflows` is deliberately absent: a
# workflow-file change pushed to a branch runs in CI, with access to the
# repository's secrets, before any human reviews the PR. The payload check
# in receiver.autofix rejects such paths before any call is made, and this
# scope is the enforcement behind it: a token minted from this dict cannot
# push one.
AUTOFIX_PERMISSIONS = {"contents": "write", "pull_requests": "write"}

# Every file a fix carries is written as a regular, non-executable blob. A
# fix that needs to change a file's mode is not a contained fix.
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

    def _installation_token(self, repo: str, permissions: dict[str, str]) -> dict:
        """Mint a token for `repo`'s installation, downscoped to exactly
        `permissions` and to that one repository. Raises on failure."""
        app_jwt = self._app_jwt()
        lookup = self.session.get(
            f"{API_BASE}/repos/{repo}/installation",
            headers=self._headers(app_jwt),
            timeout=TIMEOUT_SECONDS,
        )
        lookup.raise_for_status()
        minted = self.session.post(
            f"{API_BASE}/app/installations/{lookup.json()['id']}/access_tokens",
            headers=self._headers(app_jwt),
            json={
                "repositories": [repo.split("/", 1)[1]],
                "permissions": permissions,
            },
            timeout=TIMEOUT_SECONDS,
        )
        minted.raise_for_status()
        return minted.json()

    def mint_autofix_token(self, repo: str) -> MintedToken | None:
        """A one-hour token scoped to `repo` with AUTOFIX_PERMISSIONS.

        None on any failure. Never raises: the caller sits in the callback
        request path, where an exception would leave a claimed record
        unsettled.
        """
        try:
            body = self._installation_token(repo, AUTOFIX_PERMISSIONS)
            return MintedToken(
                token=body["token"], expires_at=str(body.get("expires_at") or "")
            )
        except Exception as exc:  # noqa: BLE001 - auth/transport must classify, not crash
            LOG.error("autofix token mint failed for %s: %s", repo, exc)
            return None

    def _call(self, method: str, url: str, headers: dict[str, str], **kwargs) -> dict:
        """One GitHub call, raising on any non-2xx so a sequence stops at
        its first failure and the guard around it reports that one."""
        got = getattr(self.session, method)(
            url, headers=headers, timeout=TIMEOUT_SECONDS, **kwargs
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

        Six calls against the Git Data API and none against a working tree.
        The tree is created with `base_tree`, so it inherits every entry of
        the base commit and overrides only the listed paths: a three-file
        fix costs three blob calls whatever the repository's size. Nothing
        here sets a commit author, so GitHub records the App installation,
        which is what makes the pull request show the App as its author.
        A failure after the ref call leaves that branch behind; the
        runbook says how to find one, and a retry is a re-fire with a
        fresh dispatch id and therefore a fresh branch name.
        """
        minted = self.mint_autofix_token(repo)
        if minted is None:
            return None
        headers = self._headers(minted.token)
        base = f"{API_BASE}/repos/{repo}"
        try:
            # Confirms the pull request's base exists; its SHA is not used,
            # because the fix was written against `base_sha`, not the tip.
            self._call("get", f"{base}/git/ref/heads/{base_branch}", headers)
            parent = self._call("get", f"{base}/git/commits/{base_sha}", headers)
            entries = []
            for path, content in files:
                blob = self._call(
                    "post", f"{base}/git/blobs", headers,
                    json={"content": content, "encoding": "utf-8"},
                )
                entries.append(
                    {"path": path, "mode": BLOB_MODE, "type": "blob", "sha": blob["sha"]}
                )
            tree = self._call(
                "post", f"{base}/git/trees", headers,
                json={"base_tree": parent["tree"]["sha"], "tree": entries},
            )
            commit = self._call(
                "post", f"{base}/git/commits", headers,
                json={"message": title, "tree": tree["sha"], "parents": [base_sha]},
            )
            self._call(
                "post", f"{base}/git/refs", headers,
                json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )
            pull = self._call(
                "post", f"{base}/pulls", headers,
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
