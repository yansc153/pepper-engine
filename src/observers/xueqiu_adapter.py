"""Xueqiu 达人 (talents) tab adapter — Playwright-based.

Architecture note (2026-05-18 final): xueqiu's public_timeline_by_category
HTTP API returns topic-card streams only, never real user statuses. The 达人
section at https://xueqiu.com/today is the canonical "expert user posts" feed
but it's JS-rendered and requires clicking the 达人 tab to refresh — so we
drive it via Playwright (same pattern as futu).

Each post in 达人 is a long-form column with body + image + author handle +
fav/retweet/reply counts — exactly the "rewritable original" the writer needs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from observers.base import (
    Observation,
    ObservationValidationError,
    from_scrape_dict,
)

logger = logging.getLogger(__name__)

XUEQIU_HOME_URL = "https://xueqiu.com/"
XUEQIU_TALENTS_URL = "https://xueqiu.com/today"  # 达人 tab lives here
XUEQIU_FEED_URL = XUEQIU_TALENTS_URL  # alias kept for runner backward-compat
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MIN_CONTENT_LENGTH = 250  # ~250 Chinese chars; column-grade only
PAGE_TIMEOUT_MS = 25000
SCROLL_PASSES = 3  # scroll a few times to load more posts via infinite scroll


class XueqiuAdapter:
    """Playwright-based xueqiu 达人 tab adapter."""

    name = "xueqiu"
    cookie_env_key = "XUEQIU_COOKIE_FILE"
    rate_limit_per_hour = 24

    def __init__(
        self,
        feed_url: str = XUEQIU_TALENTS_URL,
        tier_default: int = 0,  # tier=0: topic source only, NOT learned
        max_posts_per_fetch: int = 20,
        page_timeout_ms: int = PAGE_TIMEOUT_MS,
    ) -> None:
        self._feed_url = feed_url
        self._tier_default = tier_default
        self._max_posts = max_posts_per_fetch
        self._page_timeout_ms = page_timeout_ms

    async def fetch_latest(self, since: datetime) -> list[Observation]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        try:
            raw_posts = await self._fetch_via_browser()
        except Exception as exc:  # noqa: BLE001
            logger.warning("xueqiu fetch failed: %s", exc)
            return []
        return self._parse_posts(raw_posts, since)

    async def health_check(self) -> bool:
        return self._cookie_file().exists()

    # ------------------------------------------------------------------

    def _cookie_file(self) -> Path:
        path = os.environ.get(self.cookie_env_key, "")
        if not path:
            return Path("/app/secrets/xueqiu_cookies.json")
        return Path(path)

    def _load_cookies(self) -> list[dict[str, Any]]:
        path = self._cookie_file()
        if not path.exists():
            raise FileNotFoundError(f"xueqiu cookie file missing: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        # Playwright wants a list of cookie dicts with domain/path
        return raw if isinstance(raw, list) else []

    async def _fetch_via_browser(self) -> list[dict[str, Any]]:
        """Drive headless Chromium: open 达人 page, click refresh, scrape cards.

        Returns a list of raw dicts compatible with from_scrape_dict.
        """
        from playwright.async_api import async_playwright

        results: list[dict[str, Any]] = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(user_agent=DEFAULT_USER_AGENT)
                try:
                    cookies = await asyncio.to_thread(self._load_cookies)
                    await ctx.add_cookies(cookies)
                    logger.info("xueqiu loaded %d cookies", len(cookies))
                except (FileNotFoundError, TypeError) as exc:
                    logger.warning("xueqiu cookies not loaded: %s", exc)

                page = await ctx.new_page()
                await page.goto(self._feed_url, timeout=self._page_timeout_ms)

                # Click "达人" tab to force-refresh the feed (user direction)
                for sel in ("text=达人", '[role="tab"]:has-text("达人")',
                            'a:has-text("达人")'):
                    try:
                        await page.click(sel, timeout=3000)
                        break
                    except Exception:  # noqa: BLE001
                        continue
                else:
                    logger.debug("xueqiu 达人 tab not found, scraping current view")

                await page.wait_for_load_state("networkidle", timeout=8000)

                # Scroll to trigger lazy-load
                for _ in range(SCROLL_PASSES):
                    await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                    await page.wait_for_timeout(800)

                # Try several known card selectors — xueqiu uses .AnonymousHome__list__item
                # and .timeline__item across different layouts; cast a wide net.
                CARDS_SELECTORS = (
                    ".AnonymousHome__list__item",
                    ".timeline__item",
                    "article",
                    "[class*='timeline'] [class*='item']",
                    "[class*='status-card']",
                )
                cards = None
                used = "none"
                count = 0
                for sel in CARDS_SELECTORS:
                    locator = page.locator(sel)
                    n = await locator.count()
                    if n > 0:
                        cards = locator
                        used = sel
                        count = n
                        break

                title = await page.title()
                logger.info(
                    "xueqiu page loaded, selector=%s match=%d, title=%s",
                    used, count, title,
                )
                if count == 0:
                    probe = await page.evaluate(
                        """() => {
                            const cls = new Set();
                            for (const el of document.querySelectorAll('div, article, section, li')) {
                                if (el.children.length >= 1 && el.innerText && el.innerText.length > 30) {
                                    if (el.className && typeof el.className === 'string') {
                                        cls.add(el.className.split(' ')[0]);
                                    }
                                    if (cls.size >= 30) break;
                                }
                            }
                            return Array.from(cls).slice(0, 30);
                        }"""
                    )
                    logger.info("xueqiu DOM probe candidate classes: %s", probe)

                if cards is None:
                    return results

                for i in range(min(count, self._max_posts)):
                    try:
                        card = cards.nth(i)
                        results.append(await self._extract_card(card, page.url))
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("xueqiu skip card %d: %s", i, exc)
            finally:
                await browser.close()
        return results

    @staticmethod
    async def _extract_card(card: Any, page_url: str) -> dict[str, Any]:
        text = (await card.inner_text()).strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            raise ValueError("card has too few lines")
        handle = lines[0][:60]
        body = "\n".join(lines[1:])[:5000]

        # link to the post — first <a> with href
        href = ""
        link = card.locator("a").first
        if await link.count() > 0:
            href = await link.get_attribute("href") or ""
        if href and not href.startswith("http"):
            href = "https://xueqiu.com" + href

        # cover image — first <img> inside the card
        image_url = ""
        img = card.locator("img").first
        if await img.count() > 0:
            src = await img.get_attribute("src") or ""
            if src.startswith("http"):
                image_url = src

        return {
            "author_handle": handle,
            "content": body,
            "posted_at": datetime.now(timezone.utc),  # 达人 page hides exact time
            "likes": 0,
            "retweets": 0,
            "replies": 0,
            "has_image": bool(image_url),
            "raw_url": href or page_url,
            "image_url": image_url,
        }

    def _parse_posts(
        self, raw_posts: list[dict[str, Any]], since: datetime
    ) -> list[Observation]:
        out: list[Observation] = []
        for raw in raw_posts[: self._max_posts]:
            if not raw.get("image_url"):
                logger.debug("xueqiu skip text-only card")
                continue
            content = (raw.get("content") or "").strip()
            if len(content) < MIN_CONTENT_LENGTH:
                logger.debug("xueqiu skip short card (%d chars)", len(content))
                continue
            try:
                obs = from_scrape_dict(
                    raw, source=self.name, tier=self._tier_default  # type: ignore[arg-type]
                )
            except ObservationValidationError as exc:
                logger.debug("xueqiu skip card: %s", exc)
                continue
            if obs.posted_at <= since:
                continue
            out.append(obs)
        return out
