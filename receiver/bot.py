"""Bot Framework REST v3 client for posting Adaptive Cards into Teams."""

from __future__ import annotations

import logging
import time

import requests

LOG = logging.getLogger(__name__)
TIMEOUT_SECONDS = 10
BOT_SCOPE = "https://api.botframework.com/.default"
ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"
# Re-fetch this many seconds before AAD's stated expiry, so a token cannot go
# stale mid-request.
EXPIRY_MARGIN_SECONDS = 300
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600


class BotError(RuntimeError):
    """A Bot Framework call failed. Never swallowed: the caller returns 500."""


class TeamsBotClient:
    """Posts cards and thread replies as the notification-only bot identity."""

    def __init__(self, tenant_id: str, app_id: str, app_password: str, service_url: str):
        self.tenant_id = tenant_id
        self.app_id = app_id
        self.app_password = app_password
        self.service_url = service_url.rstrip("/")
        self.session = requests.Session()
        self._token: str | None = None
        # None means "lifetime not managed here" — a token injected directly by
        # a caller. A token this client fetched always carries a real expiry.
        self._token_expires_at: float | None = None

    def token(self) -> str:
        """Return a client-credentials token, cached until shortly before expiry.

        Lambda containers outlive an AAD token, so caching forever would make a
        warm container 401 on every post until it was recycled.
        """
        if self._token and (
            self._token_expires_at is None or time.monotonic() < self._token_expires_at
        ):
            return self._token

        response = self.session.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.app_id,
                "client_secret": self.app_password,
                "scope": BOT_SCOPE,
            },
            timeout=TIMEOUT_SECONDS,
        )
        if not response.ok:
            raise BotError(f"token request failed: HTTP {response.status_code}")

        body = response.json()
        token = body["access_token"]

        # Compute the expiry BEFORE caching the token. If this raised after the
        # assignment, the token would be cached with no expiry — which is exactly
        # the cache-forever bug this method exists to prevent.
        # Explicit None check: `or` would treat a literal expires_in of 0 as absent.
        stated = body.get("expires_in")
        try:
            lifetime = float(DEFAULT_TOKEN_LIFETIME_SECONDS if stated is None else stated)
        except (TypeError, ValueError):
            LOG.warning("AAD returned an unusable expires_in (%r); assuming default", stated)
            lifetime = float(DEFAULT_TOKEN_LIFETIME_SECONDS)

        self._token_expires_at = time.monotonic() + max(lifetime - EXPIRY_MARGIN_SECONDS, 0)
        self._token = token
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}

    def _post_authed(self, url: str, payload: dict):
        """POST with a bearer token, refreshing it once if the call comes back 401.

        A rejected token is the one failure worth retrying: the alternative is a
        warm container that 500s every alert until Lambda recycles it.
        """
        response = self.session.post(
            url, headers=self._headers(), json=payload, timeout=TIMEOUT_SECONDS
        )
        if response.status_code != 401:
            return response

        LOG.info("bot token rejected; refreshing and retrying once")
        self._token = None
        self._token_expires_at = None
        return self.session.post(
            url, headers=self._headers(), json=payload, timeout=TIMEOUT_SECONDS
        )

    def post_card(self, channel_id: str, card: dict, summary: str) -> tuple[str, str]:
        """Create a channel conversation carrying `card`.

        Returns (conversation_id, message_id). The message id is what a later
        thread reply addresses, so it is captured at post time, not looked up.
        """
        payload = {
            "isGroup": True,
            "channelData": {"channel": {"id": channel_id}},
            "activity": {
                "type": "message",
                "summary": summary,
                "attachments": [
                    {"contentType": ADAPTIVE_CARD_CONTENT_TYPE, "content": card}
                ],
            },
        }
        response = self._post_authed(f"{self.service_url}/v3/conversations", payload)
        if not response.ok:
            raise BotError(
                f"conversation create failed: HTTP {response.status_code} {response.text}"
            )

        body = response.json()
        return body["id"], body.get("activityId") or body["id"]

    def reply_in_thread(self, conversation_id: str, message_id: str, text: str) -> str:
        """Post `text` as a native reply under an existing card.

        The investigation engine delivers its findings through this method; the receiver exercises it once
        in the bot smoke script so the mechanics are proven before then.
        """
        base = conversation_id.split(";", 1)[0]
        response = self._post_authed(
            f"{self.service_url}/v3/conversations/{base};messageid={message_id}/activities",
            {"type": "message", "text": text},
        )
        if not response.ok:
            raise BotError(f"thread reply failed: HTTP {response.status_code} {response.text}")

        return response.json()["id"]

    def reply_card_in_thread(
        self, conversation_id: str, message_id: str, card: dict, summary: str
    ) -> str:
        """Post an Adaptive Card as a native reply under an existing card.

        Findings replies use this rather than `reply_in_thread` so their
        detail can collapse behind Action.ToggleVisibility; `summary` feeds
        the toast notification, which cannot render a card.
        """
        base = conversation_id.split(";", 1)[0]
        response = self._post_authed(
            f"{self.service_url}/v3/conversations/{base};messageid={message_id}/activities",
            {
                "type": "message",
                "summary": summary,
                "attachments": [
                    {"contentType": ADAPTIVE_CARD_CONTENT_TYPE, "content": card}
                ],
            },
        )
        if not response.ok:
            raise BotError(
                f"thread card reply failed: HTTP {response.status_code} {response.text}"
            )

        return response.json()["id"]
