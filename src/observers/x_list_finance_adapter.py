"""X List (finance) observer adapter.

Wraps ``twitter_bot.scrape_list_by_url`` for a single Twitter List of finance
KOLs (tier-1 default). Returns ``Observation`` instances ready for INSERT into
``reaction_observations``.

The list URL is loaded from ``config/sources.yaml`` upstream; we expose a
constructor argument so tests can inject deterministic values without touching
the yaml. ``_fetch_via_twitter_bot`` is the seam tests monkeypatch — the real
implementation spins headless Playwright via S7's ``TwitterBot``.
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

DEFAULT_LIST_URL = "https://x.com/i/lists/2056032482127175889"


class XListFinanceAdapter:
    """Adapter for the finance Twitter List. Implements ``SourceAdapter``."""

    name = "x_list_finance"
    cookie_env_key = "TWITTER_COOKIE_FILE"
    rate_limit_per_hour = 12

    def __init__(
        self,
        list_url: str = DEFAULT_LIST_URL,
        tier_default: int = 1,
        max_posts_per_fetch: int = 30,
    ) -> None:
        self._list_url = list_url
        self._tier_default = tier_default
        self._max_posts = max_posts_per_fetch

    # ------------------------------------------------------------------
    # SourceAdapter surface
    # ------------------------------------------------------------------

    async def fetch_latest(self, since: datetime) -> list[Observation]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        try:
            raw_posts = await self._fetch_via_twitter_bot()
        except Exception as exc:  # noqa: BLE001 — adapter must not raise
            logger.warning("x_list_finance fetch failed: %s", exc)
            return []
        return self._parse_posts(raw_posts, since)

    async def health_check(self) -> bool:
        try:
            posts = await self._fetch_via_twitter_bot()
            return isinstance(posts, list)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _fetch_via_twitter_bot(self) -> list[dict[str, Any]]:
        """Open a headless TwitterBot session and scrape the list timeline.

        Tests monkeypatch this method to avoid launching Playwright.
        """
        from twitter_bot import TwitterBot  # lazy import keeps tests light

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
                logger.debug("x_list_finance skip raw: %s", exc)
                continue
            if obs.posted_at <= since:
                continue
            out.append(obs)
        return out
