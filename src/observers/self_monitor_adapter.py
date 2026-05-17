"""Self-monitor adapter — UNIFIED_SPEC §16.3.

Every 6 hours, log into the **small account** (``secrets/x_xiaohao_cookies.json``),
scrape the **main account** timeline for the last 48h, and cross-reference each
tweet against ``drafts``:

* match on ``content_hash`` where ``tweet_url IS NULL`` → backfill ``tweet_url``,
  flip ``status='published'``, set ``cross_referenced=1``.
* no match → record into ``wild_posts`` (manual / out-of-system tweets that
  should NOT enter the learning corpus).

This adapter implements ``SourceAdapter`` for interface symmetry but its
``fetch_latest`` always returns ``[]``; the runner never picks it up. The real
side-effects are performed in :meth:`reconcile`, invoked by an independent
``cron 0 */6 * * *`` entry from ``main.py``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from observers.base import Observation

logger = logging.getLogger(__name__)

TWITTER_HANDLE_ENV = "TWITTER_HANDLE"
XIAOHAO_COOKIE_PATH = "secrets/x_xiaohao_cookies.json"
DEFAULT_LOOKBACK_HOURS = 48
TWEET_URL_RE = re.compile(r"https?://(?:x|twitter)\.com/[^/]+/status/\d+")


def content_hash(text: str) -> str:
    """SHA-1 of utf-8 text — must match publisher._content_hash."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    scanned: int
    bound: int          # drafts patched with tweet_url
    wild: int           # rows written to wild_posts
    errors: int


class SelfMonitorAdapter:
    """6h cross-reference monitor for the main account timeline."""

    name = "self_monitor"
    # Read TWITTER_XIAOHAO_COOKIE_FILE first (canonical name matching
    # TWITTER_COOKIE_FILE / TWITTER_HANDLE); keep X_XIAOHAO_COOKIE_FILE
    # accepted for back-compat with early dev .env files.
    cookie_env_key = "TWITTER_XIAOHAO_COOKIE_FILE"
    cookie_env_key_legacy = "X_XIAOHAO_COOKIE_FILE"
    rate_limit_per_hour = 1  # cron drives this; rate-limit is informational

    def __init__(
        self,
        twitter_handle: str | None = None,
        cookie_file: str | os.PathLike[str] | None = None,
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
        max_posts_per_fetch: int = 50,
    ) -> None:
        self._twitter_handle = (
            twitter_handle or os.environ.get(TWITTER_HANDLE_ENV, "")
        ).lstrip("@")
        self._cookie_file = Path(
            cookie_file
            or os.environ.get(self.cookie_env_key)
            or os.environ.get(self.cookie_env_key_legacy)
            or XIAOHAO_COOKIE_PATH
        )
        self._lookback_hours = lookback_hours
        self._max_posts = max_posts_per_fetch

    # ------------------------------------------------------------------
    # SourceAdapter surface (no-op — learning runner skips this adapter)
    # ------------------------------------------------------------------

    async def fetch_latest(self, since: datetime) -> list[Observation]:  # noqa: ARG002
        return []

    async def health_check(self) -> bool:
        return self._cookie_file.exists() and bool(self._twitter_handle)

    # ------------------------------------------------------------------
    # Public side-effect entry point (called by cron)
    # ------------------------------------------------------------------

    async def reconcile(
        self,
        *,
        db_path: Path | None = None,
        timeline_fetcher: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
        now: datetime | None = None,
    ) -> ReconcileResult:
        """Scrape main-account timeline → bind to drafts or record as wild."""
        if not self._twitter_handle:
            raise RuntimeError(
                f"{TWITTER_HANDLE_ENV} env var unset — self_monitor cannot run"
            )
        clock_now = now or datetime.now(timezone.utc)
        cutoff = clock_now - timedelta(hours=self._lookback_hours)

        fetcher = timeline_fetcher or self._scrape_timeline
        try:
            tweets = await fetcher()
        except Exception as exc:  # noqa: BLE001
            logger.warning("self_monitor timeline scrape failed: %s", exc)
            return ReconcileResult(scanned=0, bound=0, wild=0, errors=1)

        scanned = bound = wild = errors = 0
        for tweet in tweets[: self._max_posts]:
            scanned += 1
            try:
                posted_at = _coerce_dt(tweet.get("created_at") or tweet.get("posted_at"))
            except ValueError as exc:
                logger.debug("self_monitor bad timestamp: %s", exc)
                errors += 1
                continue
            if posted_at < cutoff:
                continue
            text = (tweet.get("text") or tweet.get("content") or "").strip()
            url = (tweet.get("url") or tweet.get("tweet_url") or "").strip()
            if not text or not url:
                errors += 1
                continue
            if not TWEET_URL_RE.match(url):
                errors += 1
                continue
            try:
                if _bind_draft(text, url, posted_at, db_path=db_path):
                    bound += 1
                elif _record_wild(text, url, posted_at, db_path=db_path):
                    wild += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("self_monitor reconcile row failed: %s", exc)
                errors += 1

        _write_health(self.name, ok=errors == 0, err_msg=None, db_path=db_path)
        logger.info(
            "self_monitor reconcile: scanned=%d bound=%d wild=%d errors=%d",
            scanned, bound, wild, errors,
        )
        return ReconcileResult(scanned=scanned, bound=bound, wild=wild, errors=errors)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _scrape_timeline(self) -> list[dict[str, Any]]:
        """Open small-account TwitterBot, navigate to /{handle}, scrape tweets.

        Mirrors TwitterBot.scrape_list_by_url's selector strategy. Lives here
        (rather than as a method on TwitterBot) so S5b stays self-contained
        and S7's surface stays minimal.
        """
        from twitter_bot import TwitterBot  # lazy

        profile_url = f"https://x.com/{self._twitter_handle}"
        bot = TwitterBot(cookie_file=self._cookie_file)
        async with bot:
            await bot.ensure_logged_in()
            page = bot.page
            if page is None:
                return []
            await page.goto(profile_url, wait_until="load", timeout=20000)
            await page.wait_for_selector(
                TwitterBot.SELECTORS["tweet_article"], timeout=10000
            )
            articles = await page.query_selector_all(
                TwitterBot.SELECTORS["tweet_article"]
            )
            results: list[dict[str, Any]] = []
            for art in articles[: self._max_posts]:
                text_el = await art.query_selector(TwitterBot.SELECTORS["tweet_text"])
                if text_el is None:
                    continue
                text = await text_el.inner_text()
                time_el = await art.query_selector("time")
                created_at = ""
                tweet_url = ""
                if time_el is not None:
                    created_at = await time_el.get_attribute("datetime") or ""
                    perm = await time_el.evaluate("el => el.closest('a')?.href")
                    if perm:
                        tweet_url = str(perm)
                results.append(
                    {"text": text, "created_at": created_at, "url": tweet_url}
                )
            return results


# ----------------------------------------------------------------------
# DB-side helpers (module-level so tests can patch ``get_conn`` once)
# ----------------------------------------------------------------------


def _bind_draft(
    text: str, url: str, posted_at: datetime, *, db_path: Path | None
) -> bool:
    """Bind URL to an unbound draft via 3-stage matching.

    Stage 1: exact content_hash (handles re-runs and identical posts)
    Stage 2: hash of normalized text (handles whitespace/punct edits)
    Stage 3: fuzzy similarity ≥ 0.85 on candidate pool (catches single-char edits)

    Only considers drafts in ('pushed_to_discord', 'approved') status so we
    never resurrect a `candidate` row or stomp a `rejected` one.
    Returns True if a row was patched, False if no candidate matched.
    """
    from src.content_match import (
        SIMILARITY_THRESHOLD,
        content_hash,
        normalize_text,
        similarity,
    )
    from src.database import get_conn, with_retry

    target_hash = content_hash(text)
    norm_text = normalize_text(text)

    def _do() -> bool:
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            with conn:
                # Stage 1+2: hash lookup catches exact + normalized matches
                # because writer stores hash of normalized text.
                row = conn.execute(
                    "SELECT id FROM drafts "
                    "WHERE content_hash = ? AND tweet_url IS NULL "
                    "AND status IN ('pushed_to_discord', 'approved') "
                    "ORDER BY generated_at DESC LIMIT 1",
                    (target_hash,),
                ).fetchone()

                # Stage 3: fuzzy fallback — only scan recent unbound drafts.
                if row is None:
                    candidates = conn.execute(
                        "SELECT id, content FROM drafts "
                        "WHERE tweet_url IS NULL "
                        "AND status IN ('pushed_to_discord', 'approved') "
                        "AND generated_at >= datetime('now', '-7 days') "
                        "ORDER BY generated_at DESC LIMIT 50"
                    ).fetchall()
                    best_id = None
                    best_score = SIMILARITY_THRESHOLD
                    for c in candidates:
                        score = similarity(text, c["content"])
                        if score >= best_score:
                            best_score = score
                            best_id = int(c["id"])
                    if best_id is None:
                        return False
                    row_id = best_id
                else:
                    row_id = int(row["id"])

                # Pull full draft fields so we can mirror them into `posts`.
                # reviewer.review_and_update_weights() joins drafts ↔ posts on
                # tweet_url to write post_metrics_timeseries — without a posts
                # row here, the JOIN returns empty and timeseries stays 0
                # while drafts.status falsely advances to 'learned'.
                draft = conn.execute(
                    "SELECT content, content_hash, topic_lane, persona, image_path "
                    "FROM drafts WHERE id = ?",
                    (row_id,),
                ).fetchone()
                posted_iso = posted_at.astimezone(timezone.utc).isoformat()
                # UNIQUE(content_hash) on posts → IGNORE keeps idempotency
                # across multiple self_monitor passes that find the same tweet.
                ch = draft["content_hash"] or content_hash(draft["content"])
                conn.execute(
                    "INSERT OR IGNORE INTO posts "
                    "(content, content_hash, topic_lane, persona, "
                    "posted_at, tweet_url, image_path, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'published')",
                    (
                        draft["content"],
                        ch,
                        draft["topic_lane"],
                        draft["persona"],
                        posted_iso,
                        url,
                        draft["image_path"],
                    ),
                )
                conn.execute(
                    "UPDATE drafts SET tweet_url = ?, cross_referenced = 1, "
                    "status = 'published', posted_at = ? WHERE id = ?",
                    (url, posted_iso, row_id),
                )
                return True
        finally:
            conn.close()

    return with_retry(_do)


def _record_wild(
    text: str, url: str, posted_at: datetime, *, db_path: Path | None
) -> bool:
    from src.database import get_conn, with_retry

    sha = content_hash(text)

    def _do() -> bool:
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO wild_posts "
                    "(tweet_url, content, content_hash, posted_at) "
                    "VALUES (?, ?, ?, ?)",
                    (url, text, sha, posted_at.astimezone(timezone.utc).isoformat()),
                )
                return cur.rowcount > 0
        finally:
            conn.close()

    return with_retry(_do)


def _write_health(
    adapter_name: str, *, ok: bool, err_msg: str | None, db_path: Path | None
) -> None:
    from src.database import get_conn, with_retry

    def _do() -> None:
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO source_health (adapter_name, last_success_at, "
                    "consecutive_failures, last_error) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(adapter_name) DO UPDATE SET "
                    "last_success_at = CASE WHEN excluded.last_success_at IS NOT NULL "
                    "  THEN excluded.last_success_at ELSE source_health.last_success_at END, "
                    "consecutive_failures = CASE WHEN ? = 1 THEN 0 "
                    "  ELSE source_health.consecutive_failures + 1 END, "
                    "last_error = excluded.last_error",
                    (
                        adapter_name,
                        datetime.now(timezone.utc).isoformat() if ok else None,
                        0 if ok else 1,
                        err_msg,
                        1 if ok else 0,
                    ),
                )
        finally:
            conn.close()

    try:
        with_retry(_do)
    except Exception as exc:  # noqa: BLE001
        logger.warning("self_monitor health write failed: %s", exc)


def _coerce_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        normalised = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError as exc:
            raise ValueError(f"unparseable datetime: {value!r}") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise ValueError(f"unsupported datetime type: {type(value).__name__}")
