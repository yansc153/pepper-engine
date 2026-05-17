"""X List (general / tier-3 流量) observer adapter — placeholder.

Per ``config/sources.yaml`` this adapter is ``enabled: false`` until the user
supplies a concrete List URL. The implementation is identical in shape to
``XListFinanceAdapter`` so swapping in a URL later is a one-line config
change; ``fetch_latest`` short-circuits to ``[]`` while ``list_url`` is empty.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from observers.base import (
    Observation,
    ObservationValidationError,
    from_scrape_dict,
)

logger = logging.getLogger(__name__)


class XListGeneralAdapter:
    """Tier-3 流量 List adapter (disabled until URL is provided)."""

    name = "x_list_general"
    cookie_env_key = "TWITTER_COOKIE_FILE"
    rate_limit_per_hour = 12

    def __init__(
        self,
        list_url: str = "",
        tier_default: int = 3,
        max_posts_per_fetch: int = 30,
        enabled: bool = False,
    ) -> None:
        self._list_url = list_url
        self._tier_default = tier_default
        self._max_posts = max_posts_per_fetch
        self._enabled = enabled and bool(list_url)

    # ------------------------------------------------------------------
    # SourceAdapter surface
    # ------------------------------------------------------------------

    async def fetch_latest(self, since: datetime) -> list[Observation]:
        if not self._enabled:
            return []
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        try:
            raw_posts = await self._fetch_via_twitter_bot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("x_list_general fetch failed: %s", exc)
            return []
        return self._parse_posts(raw_posts, since)

    async def health_check(self) -> bool:
        if not self._enabled:
            # A disabled adapter is "healthy" by definition — runner uses
            # source_health to spot brokenness, not deliberate suspensions.
            return True
        try:
            posts = await self._fetch_via_twitter_bot()
            return isinstance(posts, list)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _fetch_via_twitter_bot(self) -> list[dict[str, Any]]:
        from twitter_bot import TwitterBot

        bot = TwitterBot()
        async with bot:
            await bot.ensure_logged_in()
            return await bot.scrape_list_by_url(self._list_url, max_posts=self._max_posts)

    def _parse_posts(
        self, raw_posts: list[dict[str, Any]], since: datetime
    ) -> list[Observation]:
        out: list[Observation] = []
        for raw in raw_posts[: self._max_posts]:
            try:
                obs = from_scrape_dict(
                    raw, source=self.name, tier=self._tier_default  # type: ignore[arg-type]
                )
            except ObservationValidationError as exc:
                logger.debug("x_list_general skip raw: %s", exc)
                continue
            if obs.posted_at <= since:
                continue
            out.append(obs)
        return out
