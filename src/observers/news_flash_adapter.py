"""Eastmoney news-flash adapter (tier 0, facts only — never enters learning corpus).

Pure HTTP, no cookies required. Output Observation rows are stored alongside
KOL observations but Pattern Miner filters by ``author_tier > 0`` so these
never get distilled.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from observers.base import (
    Observation,
    ObservationValidationError,
    from_scrape_dict,
)

logger = logging.getLogger(__name__)

EASTMONEY_KUAIXUN_URL = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


class NewsFlashAdapter:
    """Adapter for Eastmoney kuaixun (and pluggable secondary news flashes)."""

    name = "news_flash"
    cookie_env_key = ""  # no auth required
    rate_limit_per_hour = 30

    def __init__(
        self,
        feed_url: str = EASTMONEY_KUAIXUN_URL,
        tier_default: int = 0,
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("news_flash fetch failed: %s", exc)
            return []
        return self._parse_payload(payload, since)

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(self._feed_url, headers=self._headers())
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://kuaixun.eastmoney.com/",
        }

    async def _fetch_payload(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(self._feed_url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    def _parse_payload(
        self, payload: dict[str, Any], since: datetime
    ) -> list[Observation]:
        items = self._extract_items(payload)
        out: list[Observation] = []
        for raw in items[: self._max_posts]:
            try:
                obs = self._row_to_observation(raw)
            except ObservationValidationError as exc:
                logger.debug("news_flash skip row: %s", exc)
                continue
            if obs.posted_at <= since:
                continue
            out.append(obs)
        return out

    @staticmethod
    def _extract_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        # Eastmoney returns either {"data": {"list": [...]}} or {"list": [...]}
        if "data" in payload and isinstance(payload["data"], dict):
            return payload["data"].get("list") or []
        return payload.get("list") or []

    def _row_to_observation(self, raw: dict[str, Any]) -> Observation:
        title = (raw.get("title") or "").strip()
        digest = (raw.get("digest") or raw.get("summary") or "").strip()
        content = f"{title}\n{digest}".strip()
        if not content:
            raise ObservationValidationError("empty news flash content")

        url = raw.get("url_unique") or raw.get("url") or raw.get("link") or ""
        if not url:
            raise ObservationValidationError("missing url")

        ts = (
            raw.get("showtime")
            or raw.get("pub_time")
            or raw.get("posted_at")
            or raw.get("created_at")
        )
        if ts is None:
            raise ObservationValidationError("missing timestamp")

        # Eastmoney ships local time strings like "2026-01-01 09:00:00" — treat
        # as UTC+8 (Asia/Shanghai) per news-flash convention.
        if isinstance(ts, str) and "T" not in ts and len(ts) >= 19:
            try:
                naive = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                ts = naive.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        return from_scrape_dict(
            {
                "author_handle": "eastmoney_kuaixun",
                "content": content,
                "posted_at": ts,
                "likes": 0,
                "retweets": 0,
                "replies": 0,
                "impressions": None,
                "has_image": False,
                "raw_url": url,
            },
            source=self.name,
            tier=self._tier_default,  # type: ignore[arg-type]
        )
