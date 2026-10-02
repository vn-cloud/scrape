"""Sends the report through the Telegram Bot API."""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
ATTEMPTS = 4


class TelegramError(RuntimeError):
    pass


class TelegramBot:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.token = token
        self.session = session or requests.Session()

    def _call(self, method: str, payload: dict) -> dict:
        url = API.format(token=self.token, method=method)
        problem = ""
        for attempt in range(1, ATTEMPTS + 1):
            try:
                resp = self.session.post(url, json=payload, timeout=30)
            except requests.RequestException as exc:
                # The exception text contains the URL, i.e. the bot token. Never let it reach the logs.
                problem = f"network error ({type(exc).__name__})"
            else:
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                if data.get("ok"):
                    return data["result"]
                problem = f"HTTP {resp.status_code} {data.get('description', '')}".strip()
                if resp.status_code == 429:
                    wait = int((data.get("parameters") or {}).get("retry_after", 5))
                    log.warning("Telegram rate limit, waiting %ss", wait)
                    time.sleep(wait + 1)
                    continue
                if resp.status_code < 500:
                    break  # bad token, wrong chat id, broken HTML: retrying will not help
            if attempt < ATTEMPTS:
                log.warning("Telegram %s failed (%s), retrying", method, problem)
                time.sleep(2 ** attempt)
        raise TelegramError(f"{method}: {problem}")

    def send(self, chat_id: str, text: str) -> None:
        self._call("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        })
