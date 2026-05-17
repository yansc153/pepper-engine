"""Futu Niu-Niu Quan recommendation feed adapter.

Per spec §16.7, the recommend tab does not refresh on plain navigation — we
must click the "推荐" tab inside a Playwright browser context before scraping.

Tests mock ``_fetch_via_browser`` to avoid spinning Playwright; the public
``fetch_latest`` / ``health_check`` surface mirrors ``observers.base.SourceAdapter``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from observers.base import (
    Observation,
    ObservationValidationError,
    from_scrape_dict,
)

logger = logging.getLogger(__name__)

FUTU_FEED_URL = "https://q.futunn.com/nnq/recommend"


class FutuAdapter:
    """Adapter for Futu's Niu-Niu Quan recommended feed.

    Implements ``observers.base.SourceAdapter``.
    """

    name = "futu"
    cookie_env_key = "FUTU_COOKIE_FILE"
    rate_limit_per_hour = 12

    def __init__(
        self,
        feed_url: str = FUTU_FEED_URL,
        tier_default: int = 2,
        max_posts_per_fetch: int = 30,
        page_timeout_ms: int = 20000,
    ) -> None:
        self._feed_url = feed_url
        self._tier_default = tier_default
        self._max_posts = max_posts_per_fetch
        self._page_timeout_ms = page_timeout_ms

    # ------------------------------------------------------------------
    # SourceAdapter surface
    # ------------------------------------------------------------------

    async def fetch_latest(self, since: datetime) -> list[Observation]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        try:
            raw_posts = await self._fetch_via_browser()
        except Exception as exc:  # noqa: BLE001
            logger.warning("futu fetch failed: %s", exc)
            return []
        return self._parse_posts(raw_posts, since)

    async def health_check(self) -> bool:
        try:
            self._load_cookies()
        except FileNotFoundError:
            return False
        # Browser-based: success of a fetch is the true signal.
        try:
            posts = await self._fetch_via_browser()
            return isinstance(posts, list)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _cookie_file(self) -> Path:
        path = os.environ.get(self.cookie_env_key, "")
        if not path:
            raise FileNotFoundError(f"env {self.cookie_env_key} is unset")
        return Path(path)

    def _load_cookies(self) -> list[dict[str, Any]]:
        path = self._cookie_file()
        if not path.exists():
            raise FileNotFoundError(f"cookie file missing: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("futu cookies file must be a list")
        return data

    async def _fetch_via_browser(self) -> list[dict[str, Any]]:
        """Spin headless Playwright, click 推荐 tab, scrape post cards.

        Returns a list of raw dicts compatible with ``from_scrape_dict``.
        Tests should monkeypatch this method to avoid launching a browser.
        """
        # Import lazily so test envs without playwright still load the module.
        from playwright.async_api import async_playwright

        cookies = self._load_cookies()
        results: list[dict[str, Any]] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context()
                await ctx.add_cookies(cookies)
                page = await ctx.new_page()
                await page.goto(self._feed_url, timeout=self._page_timeout_ms)
                # §16.7: click 推荐 tab to force refresh.
                try:
                    await page.click("text=推荐", timeout=5000)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("futu 推荐 click failed (continuing): %s", exc)
                await page.wait_for_load_state("networkidle")

                cards = page.locator("[data-feed-id], article, .feed-item")
                count = min(await cards.count(), self._max_posts)
                for idx in range(count):
                    card = cards.nth(idx)
                    try:
                        results.append(await self._extract_card(card))
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("futu skip card %d: %s", idx, exc)
                        continue
            finally:
                await browser.close()
        return results

    @staticmethod
    async def _extract_card(card: Any) -> dict[str, Any]:
        text = (await card.inner_text()).strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            raise ValueError("card has insufficient text")
        handle = lines[0]
        content = "\n".join(lines[1:6])[:1200]
        href = await card.locator("a").first.get_attribute("href")
        if not href:
            raise ValueError("missing post url")
        url = href if href.startswith("http") else f"https://q.futunn.com{href}"
        img_count = await card.locator("img").count()
        return {
            "author_handle": handle,
            "content": content,
            "posted_at": datetime.now(timezone.utc),
            "likes": 0,
            "retweets": 0,
            "replies": 0,
            "has_image": img_count > 0,
            "raw_url": url,
        }

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
                logger.debug("futu skip raw: %s", exc)
                continue
            if obs.posted_at <= since:
                continue
            out.append(obs)
        return out
