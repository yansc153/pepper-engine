"""Discord 审批闸门 — pull-mode bot driven by cron.

UNIFIED_SPEC §16.1 state machine entry point. No long-lived WebSocket; every
call is one-shot HTTPS against Discord's v10 REST API so the process can exit
between cron ticks.

Two public APIs:

* :func:`push_draft_to_discord` — POST candidate draft to channel + seed
  ✅/❌/🔄 reactions. Records ``discord_message_id`` and flips
  ``drafts.status`` to ``pushed_to_discord``.
* :func:`poll_reactions` — sweep open drafts, fetch their reaction lists, and
  dispatch the *first* owner reaction to the matching handler.

The module deliberately stays thin: business logic for each reaction lives in
``publisher_callback`` / ``rejection_pool`` / ``revise_handler``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from dotenv import load_dotenv

from src.database import get_conn

LOGGER = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
APPROVE_EMOJI = "✅"  # ✅
REJECT_EMOJI = "❌"  # ❌
REVISE_EMOJI = "\U0001f504"  # 🔄
SEEDED_EMOJIS: tuple[str, ...] = (APPROVE_EMOJI, REJECT_EMOJI, REVISE_EMOJI)

_SECRETS_PATH = Path(__file__).resolve().parents[2] / "secrets" / "discord.env"


# --------------------------------------------------------------------------- #
# Config


class DiscordConfigError(RuntimeError):
    """Raised when required Discord credentials are missing."""


def _load_secrets() -> None:
    """Load secrets/discord.env into the process env (idempotent)."""
    if _SECRETS_PATH.exists():
        load_dotenv(_SECRETS_PATH, override=False)


def _require_env(key: str) -> str:
    _load_secrets()
    value = os.environ.get(key)
    if not value:
        raise DiscordConfigError(f"missing required env: {key}")
    return value


def _get_token() -> str:
    return _require_env("DISCORD_BOT_TOKEN")


def _get_channel_id() -> str:
    return _require_env("DISCORD_DRAFT_CHANNEL_ID")


def _get_owner_id() -> str | None:
    """Owner user id is *required* for ack but missing-during-dev is tolerated."""
    _load_secrets()
    return os.environ.get("DISCORD_OWNER_USER_ID")


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN") == "1"


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {_get_token()}",
        "Content-Type": "application/json",
        "User-Agent": "PepperBot (https://github.com/local, 1.0)",
    }


# --------------------------------------------------------------------------- #
# REST helpers (single-shot, no WS)


async def _post_message(
    channel_id: str, content: str, image_path: str | None = None
) -> dict[str, Any]:
    """POST a message. If image_path given, send multipart with file attached.

    Image attachment lets you preview the draft visually in Discord on phone +
    one-click download to post manually on X.
    """
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    if image_path and Path(image_path).exists():
        # multipart payload: payload_json + files[0]
        with open(image_path, "rb") as fh:
            file_bytes = fh.read()
        files = {
            "payload_json": (
                None,
                json.dumps({"content": content}),
                "application/json",
            ),
            "files[0]": (Path(image_path).name, file_bytes, "image/jpeg"),
        }
        # multipart must not set Content-Type (httpx generates boundary)
        headers = {k: v for k, v in _auth_headers().items() if k != "Content-Type"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, files=files)
            response.raise_for_status()
            return response.json()
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            url, headers=_auth_headers(), json={"content": content}
        )
        response.raise_for_status()
        return response.json()


async def _put_reaction(channel_id: str, message_id: str, emoji: str) -> None:
    encoded = httpx.QueryParams({"_": emoji}).get("_")  # url-encode helper
    url = (
        f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/reactions/"
        f"{encoded}/@me"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.put(url, headers=_auth_headers())
        response.raise_for_status()


async def _fetch_reaction_users(
    channel_id: str, message_id: str, emoji: str
) -> list[dict[str, Any]]:
    encoded = httpx.QueryParams({"_": emoji}).get("_")
    url = (
        f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/reactions/"
        f"{encoded}"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers=_auth_headers())
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return response.json()


# --------------------------------------------------------------------------- #
# Push


def _format_draft_message(row: sqlite3.Row) -> str:
    header = (
        f"**Draft #{row['id']}** · `{row['content_mode']}/{row['optimal_length']}` "
        f"· lane=`{row['topic_lane']}`"
    )
    body = row["content"]
    footer = (
        f"\nReact: {APPROVE_EMOJI} approve  {REJECT_EMOJI} reject  "
        f"{REVISE_EMOJI} regenerate"
    )
    return f"{header}\n\n{body}{footer}"


async def push_draft_to_discord(
    draft_id: int,
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Send a candidate draft to Discord and seed approval reactions.

    Returns the Discord message id. Updates ``drafts.discord_message_id`` and
    sets ``status='pushed_to_discord'``. No-op + returns existing message id
    when the draft is already pushed.
    """
    owns = conn is None
    if conn is None:
        conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"draft {draft_id} not found")
        if row["status"] != "candidate":
            existing = row["discord_message_id"] or ""
            LOGGER.info(
                "draft %s already in status=%s, skip push", draft_id, row["status"]
            )
            return existing

        channel_id = _get_channel_id()
        body = _format_draft_message(row)

        if _is_dry_run():
            message_id = f"dryrun-{draft_id}"
            LOGGER.info("[DRY_RUN] would push draft %s: %s", draft_id, body[:120])
        else:
            img_path = row["image_path"] if "image_path" in row.keys() else None
            message = await _post_message(channel_id, body, image_path=img_path)
            message_id = str(message["id"])
            for emoji in SEEDED_EMOJIS:
                try:
                    await _put_reaction(channel_id, message_id, emoji)
                except httpx.HTTPError as exc:  # pragma: no cover - best effort
                    LOGGER.warning("seed reaction %s failed: %s", emoji, exc)

        with conn:
            conn.execute(
                "UPDATE drafts SET status='pushed_to_discord', "
                "discord_message_id=? WHERE id=?",
                (message_id, draft_id),
            )
        return message_id
    finally:
        if owns:
            conn.close()


# --------------------------------------------------------------------------- #
# Poll


ReactionHandler = Callable[[int, sqlite3.Connection], Awaitable[None]]


async def _resolve_handlers() -> dict[str, ReactionHandler]:
    """Lazy import to keep test isolation simple."""
    from src.discord.publisher_callback import handle_approval
    from src.discord.rejection_pool import handle_rejection
    from src.discord.revise_handler import handle_revise

    return {
        APPROVE_EMOJI: handle_approval,
        REJECT_EMOJI: handle_rejection,
        REVISE_EMOJI: handle_revise,
    }


async def _detect_owner_reaction(
    channel_id: str, message_id: str, owner_id: str
) -> str | None:
    """Return the first owner-attributed emoji among the three, else None."""
    for emoji in SEEDED_EMOJIS:
        users = await _fetch_reaction_users(channel_id, message_id, emoji)
        if any(str(u.get("id")) == owner_id for u in users):
            return emoji
    return None


async def poll_reactions(
    since_minutes: int = 10,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Sweep drafts in ``pushed_to_discord`` and dispatch owner reactions.

    Returns the number of drafts whose state advanced this tick. ``since_minutes``
    is accepted for cron parity but the sweep is driven by DB state, not time
    (idempotent: a draft already past ``pushed_to_discord`` is skipped).
    """
    del since_minutes  # state-driven, kept for cron API stability

    owner_id = _get_owner_id()
    if owner_id is None:
        LOGGER.warning(
            "DISCORD_OWNER_USER_ID not set, refusing to process reactions"
        )
        return 0

    channel_id = _get_channel_id()
    handlers = await _resolve_handlers()

    owns = conn is None
    if conn is None:
        conn = get_conn()
    advanced = 0
    try:
        rows = conn.execute(
            "SELECT id, discord_message_id FROM drafts "
            "WHERE status='pushed_to_discord' AND discord_message_id IS NOT NULL"
        ).fetchall()

        for row in rows:
            draft_id = int(row["id"])
            message_id = str(row["discord_message_id"])
            try:
                emoji = await _detect_owner_reaction(
                    channel_id, message_id, owner_id
                )
            except httpx.HTTPError as exc:
                LOGGER.warning("fetch reactions failed for %s: %s", draft_id, exc)
                continue
            if emoji is None:
                continue

            handler = handlers[emoji]
            try:
                await handler(draft_id, conn)
            except Exception:
                LOGGER.exception("handler %s failed on draft %s", emoji, draft_id)
                continue

            with conn:
                conn.execute(
                    "UPDATE drafts SET discord_reaction=?, "
                    "discord_reacted_at=CURRENT_TIMESTAMP WHERE id=?",
                    (emoji, draft_id),
                )
            advanced += 1
        return advanced
    finally:
        if owns:
            conn.close()


# --------------------------------------------------------------------------- #
# CLI shim (called by cron via src/main.py)


def run_poll_once(since_minutes: int = 10) -> int:
    """Sync entrypoint suitable for ``python -m src.discord.bot``."""
    return asyncio.run(poll_reactions(since_minutes))


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    print(f"advanced={run_poll_once()}")
