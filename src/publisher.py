"""
publisher.py — thin shell over TwitterBot.

Responsibilities (per UNIFIED_SPEC §5.3 + §11.4 + §16.1):
  - DRY_RUN short-circuit (env DRY_RUN=1 OR explicit kwarg)
  - 24h content_hash de-duplication against the ``posts`` table
  - If the main text contains a URL, strip it out and post the URL
    as the first reply (per §10 "主推带链接降权" rule)
  - Bubble up TwitterBot errors as PostResult.error

S13 (Discord callback) is the primary caller. S5b uses TwitterBot
directly for read-only scraping — it does not go through publisher.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

try:
    # database is provided by S1; tests stub it.
    from database import get_conn  # type: ignore
except Exception:  # pragma: no cover - import-time fallback for unit tests
    get_conn = None  # type: ignore[assignment]

from twitter_bot import NotLoggedInError, TwitterBot

logger = logging.getLogger(__name__)

# URL regex matches first http(s) link in the body; conservative — we
# only split off the LAST trailing URL, since most drafts end with one.
_URL_RE = re.compile(r"https?://\S+")


@dataclass
class PostResult:
    success: bool
    tweet_url: str | None
    error: str | None


# ── helpers ────────────────────────────────────────────────────────────────


def _is_dry_run(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get("DRY_RUN", "0") == "1"


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def is_duplicate_recent(text: str, hours: int = 24) -> bool:
    """True if a post with the same content_hash was published in the last N hours."""
    if get_conn is None:
        return False
    sha = _content_hash(text)
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM posts WHERE content_hash=? "
                "AND posted_at > datetime('now', ?)",
                (sha, f"-{hours} hours"),
            ).fetchone()
    except Exception as exc:
        # Don't block posting on DB errors; surface in logs.
        logger.warning("dedup check failed: %s", exc)
        return False
    return row is not None


def split_trailing_url(text: str) -> tuple[str, str | None]:
    """
    Return ``(main_text, url_to_reply)``.

    If the body contains a URL we strip the LAST URL out of the main
    body and return it as the reply payload. Multiple URLs collapse to
    one reply (the last one). If there is no URL, ``(text, None)``.
    """
    matches = list(_URL_RE.finditer(text))
    if not matches:
        return text, None
    last = matches[-1]
    url = last.group(0).rstrip(".,!?)")
    main = (text[: last.start()] + text[last.end():]).strip()
    main = re.sub(r"\s+", " ", main).strip()
    if not main:
        # body was just the URL — keep it inline rather than producing empty post
        return text, None
    return main, url


# ── public entry points ────────────────────────────────────────────────────


async def post_tweet(
    text: str,
    image_path: str | None = None,
    *,
    dry_run: bool | None = None,
    bot: TwitterBot | None = None,
) -> PostResult:
    """
    Publish a draft. Honors DRY_RUN, 24h content-hash dedup, and the
    URL-as-first-reply rule.

    The ``bot`` kwarg is for tests / callers that want to reuse an
    already-started TwitterBot. When None, we create + start a new bot
    and stop it before returning.
    """
    if _is_dry_run(dry_run):
        logger.info("DRY_RUN: would post %r (image=%s)", text[:80], image_path)
        return PostResult(success=True, tweet_url=None, error=None)

    if is_duplicate_recent(text):
        return PostResult(
            success=False, tweet_url=None, error="duplicate within 24h"
        )

    main_text, url_to_reply = split_trailing_url(text)

    owned = bot is None
    bot = bot or TwitterBot()
    try:
        if owned:
            await bot.start()
        try:
            await bot.ensure_logged_in()
        except NotLoggedInError as exc:
            return PostResult(
                success=False, tweet_url=None, error=f"not_logged_in: {exc}"
            )

        result: dict[str, Any] = await bot.post_tweet(main_text, image_path)
        if not result.get("success"):
            return PostResult(
                success=False,
                tweet_url=result.get("tweet_url"),
                error=result.get("error") or "post failed",
            )

        tweet_url = result.get("tweet_url")
        if url_to_reply and tweet_url:
            reply = await bot.reply_to_tweet(tweet_url, url_to_reply)
            if not reply.get("success"):
                # main post succeeded, just log; don't fail caller.
                logger.warning(
                    "main post ok but URL reply failed: %s", reply.get("error")
                )

        return PostResult(success=True, tweet_url=tweet_url, error=None)
    finally:
        if owned:
            await bot.stop()


async def get_post_metrics(
    tweet_url: str,
    *,
    bot: TwitterBot | None = None,
) -> dict[str, int]:
    """Scrape engagement counters. Mirrors twitter_bot.get_post_metrics."""
    owned = bot is None
    bot = bot or TwitterBot()
    try:
        if owned:
            await bot.start()
        try:
            await bot.ensure_logged_in()
        except NotLoggedInError:
            return {}
        return await bot.get_post_metrics(tweet_url)
    finally:
        if owned:
            await bot.stop()
