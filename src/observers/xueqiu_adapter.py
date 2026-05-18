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
    "https://xueqiu.com/v4/statuses/public_timeline_by_category.json"
    "?since_id=-1&max_id=-1&count=20&category=-1"
)
XUEQIU_HOME_URL = "https://xueqiu.com/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


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
        tier_default: int = 2,
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
            logger.info(
                "xueqiu item keys=%s | first.id=%s first.text_snippet=%s",
                list(first.keys()),
                first.get("id"),
                str(first.get("text", first.get("description", "")))[:80],
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
            try:
                obs = self._row_to_observation(raw)
            except ObservationValidationError as exc:
                logger.debug("xueqiu skip row: %s", exc)
                continue
            if obs.posted_at <= since:
                continue
            out.append(obs)
        return out

    def _row_to_observation(self, raw: dict[str, Any]) -> Observation:
        user = raw.get("user") or {}
        handle = (user.get("screen_name") or "").strip()
        if not handle:
            raise ObservationValidationError("missing author handle")
        target = raw.get("target") or ""
        if not target:
            raise ObservationValidationError("missing target url")
        url = target if target.startswith("http") else f"https://xueqiu.com{target}"
        pic = raw.get("pic_sizes") or raw.get("pic") or ""
        has_image = bool(pic) and pic != ""

        # xueqiu created_at is epoch milliseconds
        created_raw = raw.get("created_at")
        if isinstance(created_raw, (int, float)) and created_raw > 1e12:
            posted_at = datetime.fromtimestamp(created_raw / 1000, tz=timezone.utc)
        else:
            posted_at = created_raw  # let from_scrape_dict coerce

        return from_scrape_dict(
            {
                "author_handle": handle,
                "content": raw.get("text") or raw.get("description") or "",
                "posted_at": posted_at,
                "likes": raw.get("fav_count", 0),
                "retweets": raw.get("retweet_count", 0),
                "replies": raw.get("reply_count", 0),
                "impressions": raw.get("view_count"),
                "has_image": has_image,
                "raw_url": url,
            },
            source=self.name,
            tier=self._tier_default,  # type: ignore[arg-type]
        )
