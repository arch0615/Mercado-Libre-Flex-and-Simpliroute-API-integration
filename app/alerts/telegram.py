"""Telegram Bot API client.

The public surface is a single `post_message(title, body, level)` that
formats a plain-text message and sends it to the configured chat. Network
errors are logged but never raised to the caller — a broken alert channel
must never take down the sync job.
"""
from __future__ import annotations

from typing import Literal

import httpx

from app.core.config import get_settings
from app.core.logging import logger

AlertLevel = Literal["warning", "error", "critical"]

_ICONS = {
    "warning": "⚠️",
    "error": "🛑",
    "critical": "🚨",
}


def _api_url() -> str:
    token = get_settings().telegram_bot_token
    return f"https://api.telegram.org/bot{token}/sendMessage"


def post_message(
    title: str,
    body: str,
    level: AlertLevel = "error",
    *,
    http_client: httpx.Client | None = None,
) -> bool:
    """Send a message to the Telegram chat. Returns True on success, False otherwise.

    Never raises — alerting must not take down the job.
    """
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("telegram_not_configured")
        return False

    icon = _ICONS.get(level, "ℹ️")
    text = f"{icon} *[{level.upper()}]* {title}\n\n{body}"

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=10.0)
    try:
        response = client.post(
            _api_url(),
            json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
        if response.status_code >= 400:
            logger.warning(
                "telegram_send_failed",
                status=response.status_code,
                body=response.text[:200],
            )
            return False
        return True
    except (httpx.TimeoutException, httpx.TransportError) as e:
        logger.warning("telegram_network_error", error=str(e))
        return False
    finally:
        if owns_client:
            client.close()
