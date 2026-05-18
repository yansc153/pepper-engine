"""✅ reaction → approve draft.

Two modes (env ``DISCORD_APPROVAL_MODE``):
  - ``manual`` (default): mark status='approved' and stop. User publishes
    manually in their X client.
  - ``auto``: call publisher, write tweet_url, status='published'.

S7 owns ``src.publisher``; lazy import so manual mode never requires it.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Awaitable, Callable

LOGGER = logging.getLogger(__name__)

PostTweetFn = Callable[[str, str | None], Awaitable[str]]


async def _post_tweet(content: str, image_path: str | None) -> str:
    """Lazy proxy to S7's publisher. Patched in tests.

    publisher.post_tweet returns a PostResult dataclass; we surface only the
    tweet_url (empty string on failure) so legacy callers can treat the result
    as a string.
    """
    from src.publisher import PostResult, post_tweet  # type: ignore[import-not-found]

    result = await post_tweet(content, image_path)
    # Tests may monkeypatch this function to return a bare string directly.
    if isinstance(result, PostResult):
        return result.tweet_url or ""
    return result or ""


async def handle_approval(draft_id: int, conn: sqlite3.Connection) -> None:
    """Approve flow. Manual mode stops at status=approved; auto mode publishes."""
    row = conn.execute(
        "SELECT content, image_path, status FROM drafts WHERE id=?",
        (draft_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"draft {draft_id} not found")
    if row["status"] not in {"pushed_to_discord", "approved"}:
        LOGGER.info(
            "draft %s in status=%s, skip approval", draft_id, row["status"]
        )
        return

    with conn:
        conn.execute(
            "UPDATE drafts SET status='approved' WHERE id=?", (draft_id,)
        )

    mode = os.environ.get("DISCORD_APPROVAL_MODE", "manual").lower()
    if mode == "manual":
        LOGGER.info("draft %s approved (manual mode); publish manually", draft_id)
        return

    tweet_url = await _post_tweet(row["content"], row["image_path"])
    if not tweet_url:
        LOGGER.error("publisher returned empty url for draft %s", draft_id)
        return

    # Pull the columns we need to mirror into `posts` so reviewer's
    # drafts ↔ posts JOIN can find the row and write metrics_timeseries.
    full = conn.execute(
        "SELECT content, content_hash, topic_lane, persona, image_path "
        "FROM drafts WHERE id=?",
        (draft_id,),
    ).fetchone()
    from src.content_match import content_hash as _ch
    ch = (full["content_hash"] if full else None) or _ch(row["content"])

    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO posts "
            "(content, content_hash, topic_lane, persona, "
            "posted_at, tweet_url, image_path, status) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, 'published')",
            (
                row["content"],
                ch,
                (full["topic_lane"] if full else None),
                (full["persona"] if full else None),
                tweet_url,
                row["image_path"],
            ),
        )
        conn.execute(
            "UPDATE drafts SET status='published', tweet_url=?, "
            "posted_at=CURRENT_TIMESTAMP WHERE id=?",
            (tweet_url, draft_id),
        )
    LOGGER.info("draft %s published → %s", draft_id, tweet_url)
