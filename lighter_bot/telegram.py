from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from settings import RETRY

from .global_config import load_global_config
from .pretty import format_user_text, warn


def _send(message: str) -> None:
    config = load_global_config().telegram
    message = format_user_text(message)
    url = f"https://api.telegram.org/bot{config.token}/sendMessage"
    body = json.dumps(
        {
            "chat_id": config.chat_id,
            "text": message,
            "link_preview_options": {"is_disabled": True},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "lighter-robinhood-bot/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("description") or "Telegram returned ok=false"))


async def send_tg(message: str) -> bool:
    message = format_user_text(message)
    config = load_global_config().telegram
    if not config.enabled:
        return False

    last_error = "unknown error"
    for attempt in range(1, max(1, RETRY) + 1):
        try:
            await asyncio.to_thread(_send, message)
            return True
        except Exception as exc:
            last_error = str(exc).replace(config.token, "<bot-token>")
            if attempt < max(1, RETRY):
                await asyncio.sleep(attempt)
    warn("Telegram failed", last_error)
    return False
