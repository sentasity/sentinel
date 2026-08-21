"""GitHub App auth and repository_dispatch client for the autofix workflow.

Modeled on receiver.routines.RoutineClient: never raises out of dispatch();
outcomes classify, the caller decides. The App's installation token is
minted per dispatch and never persisted.
"""

from __future__ import annotations

import enum
import logging
import time

import jwt
import requests

LOG = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 15
# App JWTs may live at most 10 minutes; 8 leaves clock-skew margin.
JWT_TTL_SECONDS = 8 * 60


class DispatchOutcome(enum.Enum):
    """What a dispatch attempt did, from the caller's point of view."""

    DISPATCHED = "dispatched"
    RETRYABLE = "retryable"
    REJECTED = "rejected"


class GitHubAppClient:
    """Mints installation tokens and fires repository_dispatch as the App."""

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

    def _installation_token(self, repo: str) -> str:
        """A one-hour token scoped to `repo`'s installation. Raises on failure."""
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
            timeout=TIMEOUT_SECONDS,
        )
        minted.raise_for_status()
        return minted.json()["token"]

    def dispatch(self, repo: str, event_type: str, client_payload: dict) -> DispatchOutcome:
        """Fire repository_dispatch on `repo`. Never raises.

        GitHub caps client_payload at 10 top-level properties; the autofix
        payload uses exactly 10, so any new field must replace one.
        """
        try:
            token = self._installation_token(repo)
            fired = self.session.post(
                f"{API_BASE}/repos/{repo}/dispatches",
                headers=self._headers(token),
                json={"event_type": event_type, "client_payload": client_payload},
                timeout=TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - auth/transport must classify, not crash
            LOG.warning("dispatch transport or auth error: %s", exc)
            return DispatchOutcome.RETRYABLE

        if fired.status_code == 204:
            return DispatchOutcome.DISPATCHED
        if 500 <= fired.status_code < 600:
            return DispatchOutcome.RETRYABLE
        LOG.error(
            "dispatch rejected: HTTP %s %s", fired.status_code, (fired.text or "")[:500]
        )
        return DispatchOutcome.REJECTED
