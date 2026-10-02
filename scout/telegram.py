"""Sends the report through the Telegram Bot API."""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


class TelegramBot:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.token = token
        self.session = session or requests.Session()

    def _call(self, method: str, payload: dict) -> dict:
        url = API.format(token=self.token, method=method)
        for attempt in range(4):
            try:
                resp = self.session.post(url, json=payload, timeout=30)
            except requests.RequestException as exc:
                # The exception text contains the URL, i.e. the bot token. Never let it reach the logs.
                raise TelegramError(f"{method}: network error ({type(exc).__name__})") from None
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code == 429:
                wait = int(data.get("parameters", {}).get("retry_after", 5))
                log.warning("Telegram rate limit, waiting %ss", wait)
                time.sleep(wait + 1)
                continue
            if not data.get("ok"):
                raise TelegramError(f"{method}: HTTP {resp.status_code} {data.get('description', '')}".strip())
            return data["result"]
        raise TelegramError(f"{method}: still rate limited after retries")

    def send(self, chat_id: str, text: str) -> None:
        self._call("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        })

    def find_private_chat_id(self) -> str | None:
        """If TELEGRAM_CHAT_ID is not set, use the person who most recently wrote /start to the bot."""
        updates = self._call("getUpdates", {"allowed_updates": ["message"]})
        for update in reversed(updates):
            chat = (update.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private":
                return str(chat["id"])
        return None
