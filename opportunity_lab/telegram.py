from __future__ import annotations

import json
import urllib.parse
import urllib.request


class Telegram:
    def __init__(self, token: str | None, chat_id: str | None):
        self.token = token
        self.chat_id = chat_id
        self.api = f"https://api.telegram.org/bot{token}" if token else None

    @property
    def enabled(self) -> bool:
        return bool(self.api and self.chat_id)

    def call(self, method: str, **params):
        if not self.enabled:
            return None
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(f"{self.api}/{method}", data=data, timeout=40) as raw:
            return json.loads(raw.read())

    def send(self, text: str):
        return self.call("sendMessage", chat_id=self.chat_id, text=text[:4000])

    def updates(self, offset: int, timeout: int = 20):
        response = self.call("getUpdates", offset=offset, timeout=timeout)
        return [] if response is None else response.get("result", [])

