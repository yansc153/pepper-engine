"""Discord bot REST alerting for cron failures and operational anomalies.

Reuses the existing bot token (no separate webhook needed). Posts to:
  - ALERT_CHANNEL_ID  if set, else DISCORD_DRAFT_CHANNEL_ID
  - inside ALERT_THREAD_ID  if set (lets you isolate alerts in a thread of the same channel)

Resolves all env lazily so unit tests / dry-run can run without secrets.
Alert delivery NEVER raises — a Discord outage must not turn into a cron
failure of its own.

Usage:
    from src.alerting import alert
    alert("draft #42 stuck in 'approved' for 8 days")
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
_DISCORD_MAX_LEN = 1900  # Discord message content cap is 2000


def alert(message: str, *, context: dict[str, Any] | None = None) -> bool:
    """Post ``message`` to Discord. Returns True on success.

    Never raises. Logs locally either way so the alert is recoverable from
    log files even if Discord delivery fails.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = (
        os.environ.get("ALERT_CHANNEL_ID")
        or os.environ.get("DISCORD_DRAFT_CHANNEL_ID")
        or ""
    ).strip()
    thread_id = os.environ.get("ALERT_THREAD_ID", "").strip()
    body = _format(message, context)

    if not token or not channel_id:
        logger.warning(
            "ALERT (no bot/channel configured): %s", body
        )
        return False

    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    if thread_id:
        url = f"{url}?thread_id={thread_id}"

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bot {token}",
                    "Content-Type": "application/json",
                    "User-Agent": "PepperBot-Alerting/1.0",
                },
                json={"content": body},
            )
            resp.raise_for_status()
        logger.info("alert delivered (%d chars) to channel=%s", len(body), channel_id)
        return True
    except Exception as exc:  # noqa: BLE001 — alert must never crash caller
        # Strip auth header from logs by only logging the URL minus token.
        logger.error("alert delivery failed: %s | message=%s", exc, body)
        return False


def _format(message: str, context: dict[str, Any] | None) -> str:
    parts = [f":rotating_light: **pepperbot alert** — {message}"]
    if context:
        for k, v in context.items():
            parts.append(f"  • `{k}`: {v}")
    body = "\n".join(parts)
    if len(body) > _DISCORD_MAX_LEN:
        body = body[: _DISCORD_MAX_LEN - 20] + "\n…(truncated)"
    return body
