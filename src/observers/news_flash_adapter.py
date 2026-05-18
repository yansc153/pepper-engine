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

EASTMONEY_KUAIXUN_URL = (
    "https://www.cls.cn/nodeapi/updateTelegraphList"
    "?app=CailianpressWeb&os=web&sv=8.4.6&category=&lastTime="
)
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
        self._peek(payload)
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

    def _peek(self, payload: dict[str, Any]) -> None:
        """Temp diagnostic: log payload shape so we can see what eastmoney returns."""
        keys = list(payload.keys()) if isinstance(payload, dict) else []
        data = payload.get("data") if isinstance(payload, dict) else None
        data_keys = list(data.keys()) if isinstance(data, dict) else []
        items = self._extract_items(payload)
        logger.info(
            "news_flash payload top_keys=%s data_keys=%s items=%d sample=%s",
            keys, data_keys, len(items),
            (items[0] if items else None),
        )

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
        # cls.cn returns {"data": {"roll_data": [...]}}; eastmoney legacy
        # returned {"data": {"list": [...]}} — support both shapes.
        if "data" in payload and isinstance(payload["data"], dict):
            data = payload["data"]
            return data.get("roll_data") or data.get("list") or []
        return payload.get("list") or payload.get("roll_data") or []

    def _row_to_observation(self, raw: dict[str, Any]) -> Observation:
        # cls.cn uses {title, content, shareurl, ctime} where ctime is epoch seconds.
        title = (raw.get("title") or raw.get("brief") or "").strip()
        body = (
            raw.get("content")
            or raw.get("digest")
            or raw.get("summary")
            or ""
        ).strip()
        content = (f"{title}\n{body}".strip() if title else body)
        if not content:
            raise ObservationValidationError("empty news flash content")

        url = (
            raw.get("shareurl")
            or raw.get("share_url")
            or raw.get("url_unique")
            or raw.get("url")
            or raw.get("link")
            or ""
        )
        if not url:
            raise ObservationValidationError("missing url")

        ts = (
            raw.get("ctime")
            or raw.get("modified_time")
            or raw.get("showtime")
            or raw.get("pub_time")
            or raw.get("posted_at")
            or raw.get("created_at")
        )
        if ts is None:
            raise ObservationValidationError("missing timestamp")

        # cls.cn returns ctime as epoch seconds (10-digit int)
        if isinstance(ts, (int, float)) and ts > 1e9:
            ts = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif isinstance(ts, str) and "T" not in ts and len(ts) >= 19:
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
