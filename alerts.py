"""Optional Telegram alerting. Falls back to console-only if not configured."""

from __future__ import annotations
import requests
import config


def send_telegram(message: str) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        return resp.ok
    except requests.RequestException as e:
        print(f"[alerts] Telegram send failed: {e}")
        return False


def notify(message: str) -> None:
    print(message)
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        send_telegram(message)
