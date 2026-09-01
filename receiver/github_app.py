"""GitHub App auth: mints scoped installation tokens for autofix sessions.

Modeled on receiver.routines.RoutineClient's posture: public methods never
raise; a failure returns None and the caller decides. Tokens are minted per
grant and never persisted.
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
# repository's secrets, before any human reviews the PR. The gate's
# forbidden-path check declines such findings early, but this scope is the
# enforcement: a token minted from this dict cannot push one.
AUTOFIX_PERMISSIONS = {"contents": "write", "pull_requests": "write"}


@dataclass(frozen=True)
class MintedToken:
    """One scoped installation token and when GitHub will kill it."""

    token: str
    expires_at: str  # ISO-8601, straight from the GitHub response


class GitHubAppClient:
    """Mints installation tokens scoped to one repository."""

    def __init__(self, app_id: str, private_key_pem: str):
        self.app_id = app_id
        self.private_key_pem = private_key_pem
        self.session = requests.Session()
        self._slug = ""  # lazily fetched; empty until the first successful fetch

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

        None on any failure. Never raises: minting happens inside the
        findings-delivery path, where an exception would cost the reply.
        """
        try:
            body = self._installation_token(repo, AUTOFIX_PERMISSIONS)
            return MintedToken(
                token=body["token"], expires_at=str(body.get("expires_at") or "")
            )
        except Exception as exc:  # noqa: BLE001 - auth/transport must classify, not crash
            LOG.error("autofix token mint failed for %s: %s", repo, exc)
            return None

    def app_slug(self) -> str:
        """The App's own slug; "<slug>[bot]" is the login its PRs carry.

        Fetched once per container and cached; "" on failure, and a failure
        is not cached so the next call retries.
        """
        if self._slug:
            return self._slug
        try:
            got = self.session.get(
                f"{API_BASE}/app",
                headers=self._headers(self._app_jwt()),
                timeout=TIMEOUT_SECONDS,
            )
            got.raise_for_status()
            self._slug = str(got.json().get("slug") or "")
        except Exception as exc:  # noqa: BLE001 - verification degrades, never crashes
            LOG.error("app slug fetch failed: %s", exc)
            return ""
        return self._slug

    def pr_author(self, repo: str, number: int) -> str:
        """The login that authored PR `number` in `repo`; "" on any failure.

        Read with a token downscoped to pull_requests:read: authorship
        verification must not itself hold a write credential.
        """
        try:
            token = self._installation_token(repo, {"pull_requests": "read"})["token"]
            got = self.session.get(
                f"{API_BASE}/repos/{repo}/pulls/{number}",
                headers=self._headers(token),
                timeout=TIMEOUT_SECONDS,
            )
            got.raise_for_status()
            return str((got.json().get("user") or {}).get("login") or "")
        except Exception as exc:  # noqa: BLE001 - verification degrades, never crashes
            LOG.error("pr author fetch failed for %s#%s: %s", repo, number, exc)
            return ""
