"""Xueqiu hot-feed adapter.

Pulls the public hot timeline JSON via HTTP using cookies from
``secrets/xueqiu_cookies.json`` (Playwright-format list). Returns
``Observation`` instances ready for INSERT into ``reaction_observations``.

Failures are swallowed and surfaced through ``source_health``. Designed to be
mocked with ``respx`` in tests.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from observers.base import (
    Observation,
    ObservationValidationError,
    from_scrape_dict,
)

logger = logging.getLogger(__name__)

XUEQIU_FEED_URL = (
    # category=-1 = 头条 (curated long-form columns). With type=11 it filters to
    # articles (专栏长文), not short status updates. Articles always carry an image
    # and 1.5k-3k Chinese chars — exactly the "rewritable original" we need.
    "https://xueqiu.com/v4/statuses/public_timeline_by_category.json"
    "?since_id=-1&max_id=-1&count=20&category=-1&type=11"
)

# Filter threshold: skip xueqiu items shorter than this — we only want long-form
# columns/articles as rewritable source material, not short status updates.
MIN_CONTENT_LENGTH = 250  # ~250 Chinese chars ≈ 750 bytes; enough for "column-grade" posts
XUEQIU_HOME_URL = "https://xueqiu.com/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


def _unwrap_status(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the real status object from a public_timeline_by_category wrapper.

    Priority:
      1. `original_status` — the canonical un-reposted status (preferred)
      2. `data` (dict or JSON-string) — fallback; usually a topic-card shape
      3. raw — already unwrapped
    """
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("original_status"), dict) and raw["original_status"]:
        return raw["original_status"]
    data = raw.get("data")
    if isinstance(data, str) and data.strip().startswith("{"):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            pass
    if isinstance(data, dict) and data:
        return data
    return raw


class XueqiuAdapter:
    """Adapter for Xueqiu's hot-topic feed.

    Implements ``observers.base.SourceAdapter``.
    """

    name = "xueqiu"
    cookie_env_key = "XUEQIU_COOKIE_FILE"
    rate_limit_per_hour = 24

    def __init__(
        self,
        feed_url: str = XUEQIU_FEED_URL,
        tier_default: int = 0,  # tier=0: contributes topic candidates only, NOT learned
        max_posts_per_fetch: int = 30,
        request_timeout: float = 15.0,
    ) -> None:
        self._feed_url = feed_url
        self._tier_default = tier_default
        self._max_posts = max_posts_per_fetch
        self._timeout = request_timeout

    # ------------------------------------------------------------------
    # SourceAdapter surface
    # ------------------------------------------------------------------

    async def fetch_latest(self, since: datetime) -> list[Observation]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        try:
            payload = await self._fetch_payload()
        except Exception as exc:  # noqa: BLE001 — adapter must not raise
            logger.warning("xueqiu fetch failed: %s", exc)
            return []
        items = payload.get("list") or payload.get("statuses") or []
        if items:
            first = items[0]
            # public_timeline_by_category wraps the real status; unwrap it
            unwrapped = _unwrap_status(first)
            logger.info(
                "xueqiu item wrapped_keys=%s | unwrapped_keys=%s user=%s text_snip=%s",
                list(first.keys()),
                list(unwrapped.keys())[:15] if unwrapped else [],
                (unwrapped.get("user", {}).get("screen_name") if unwrapped else None),
                str(unwrapped.get("text", unwrapped.get("description", "")))[:80] if unwrapped else "",
            )
        return self._parse_payload(payload, since)

    async def health_check(self) -> bool:
        try:
            cookies = self._load_cookies()
        except FileNotFoundError:
            return False
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    XUEQIU_HOME_URL,
                    cookies=cookies,
                    headers=self._headers(),
                )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _cookie_file(self) -> Path:
        path = os.environ.get(self.cookie_env_key, "")
        if not path:
            raise FileNotFoundError(f"env {self.cookie_env_key} is unset")
        return Path(path)

    def _load_cookies(self) -> dict[str, str]:
        path = self._cookie_file()
        if not path.exists():
            raise FileNotFoundError(f"cookie file missing: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": XUEQIU_HOME_URL,
            "Accept": "application/json",
        }

    async def _fetch_payload(self) -> dict[str, Any]:
        cookies = self._load_cookies()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                self._feed_url,
                cookies=cookies,
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    def _parse_payload(
        self, payload: dict[str, Any], since: datetime
    ) -> list[Observation]:
        items = payload.get("list") or payload.get("statuses") or []
        out: list[Observation] = []
        for raw in items[: self._max_posts]:
            unwrapped = _unwrap_status(raw)
            try:
                obs = self._row_to_observation(unwrapped)
            except ObservationValidationError as exc:
                logger.debug("xueqiu skip row: %s", exc)
                continue
            if obs.posted_at <= since:
                continue
            out.append(obs)
        return out

    def _row_to_observation(self, raw: dict[str, Any]) -> Observation:
        user = raw.get("user") or {}
        # public_timeline_by_category headline items don't carry user;
        # fall back to "xueqiu_topic" as the handle so we still capture them.
        handle = (user.get("screen_name") or "").strip() or "xueqiu_topic"

        target = raw.get("target") or ""
        if not target:
            raise ObservationValidationError("missing target url")
        url = target if target.startswith("http") else f"https://xueqiu.com{target}"
        pic = raw.get("pic_sizes") or raw.get("pic") or raw.get("first_pic") or ""
        has_image = bool(pic) and pic != ""

        # Content priority: text (status) → description (topic) → title (headline)
        content = (
            raw.get("text")
            or raw.get("description")
            or raw.get("title")
            or raw.get("topic_desc")
            or raw.get("topic_title")
            or ""
        ).strip()
        if not content:
            raise ObservationValidationError("empty content")
        if len(content) < MIN_CONTENT_LENGTH:
            raise ObservationValidationError(
                f"too short ({len(content)} < {MIN_CONTENT_LENGTH}) — only long-form columns are usable"
            )

        # Extract image: xueqiu uses first_pic for thumbnail, pic for full;
        # both come back as URL strings (or absent on text-only posts which we skip).
        first_pic = raw.get("first_pic") or raw.get("pic") or ""
        image_url_str: str | None = first_pic if first_pic and first_pic.startswith("http") else None
        if not image_url_str:
            raise ObservationValidationError("no image — only image-bearing posts are usable")

        # xueqiu created_at is epoch milliseconds; topic items may lack it
        created_raw = raw.get("created_at") or raw.get("timeBefore")
        if isinstance(created_raw, (int, float)) and created_raw > 1e12:
            posted_at = datetime.fromtimestamp(created_raw / 1000, tz=timezone.utc)
        elif isinstance(created_raw, (int, float)) and created_raw > 0:
            posted_at = datetime.fromtimestamp(created_raw, tz=timezone.utc)
        else:
            # headline items often have no timestamp — use "now" so they fall
            # into the recent-observation window for topic clustering.
            posted_at = datetime.now(timezone.utc)

        return from_scrape_dict(
            {
                "author_handle": handle,
                "content": content,
                "posted_at": posted_at,
                "likes": raw.get("fav_count", 0),
                "retweets": raw.get("retweet_count", 0),
                "replies": raw.get("reply_count", 0),
                "impressions": raw.get("view_count"),
                "has_image": True,           # we already required image_url above
                "raw_url": url,
                "image_url": image_url_str,
            },
            source=self.name,
            tier=self._tier_default,  # type: ignore[arg-type]
        )
