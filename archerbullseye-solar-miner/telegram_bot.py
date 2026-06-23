import requests
from typing import Optional


class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self.token = token.strip()
        self.chat_id = str(chat_id).strip()

    def send(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            return False
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            return resp.ok
        except Exception:
            return False

    def validate(self) -> str:
        """Returns bot @username on success, error string on failure."""
        if not self.token:
            return "No bot token configured"
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.token}/getMe",
                timeout=10,
            )
            if resp.ok:
                return "@" + resp.json()["result"]["username"]
            return f"Telegram error {resp.status_code}: {resp.text[:120]}"
        except Exception as e:
            return f"Connection error: {e}"
