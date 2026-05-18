"""East Money 股吧 (guba) homepage 精选 feed adapter.

Replaces the previous per-stock list-page approach. The new flow targets the
guba homepage feed (``https://guba.eastmoney.com/``) which surfaces long-form
"精选" articles — the only format with enough body text to be worth rewriting.

Flow:

1. Playwright headless loads the homepage and waits for ``#mainlist`` to be
   populated by React (the page ships skeleton loaders until then).
2. We read the feed card list straight out of the rendered DOM and collect
   ``(title, detail_url)`` for each card.
3. Detail pages are SSR (per ``docs/GUBA_HOMEPAGE_PROBE.md``), so we fan out
   to them with ``httpx`` under a small semaphore and parse with BeautifulSoup.
4. Posts are kept only when the body is ≥ ``min_content_length`` characters
   AND contains at least one inline ``<img>`` inside ``.article-body``.

Tier 0 — topic/content source only, NEVER enters the learning corpus. The
miner filters by ``author_tier > 0`` so these rows never get distilled.

Tests mock ``_fetch_feed_cards`` and ``_fetch_detail_html`` so unit runs never
spin a real browser or touch the network.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from observers.base import (
    Observation,
    ObservationValidationError,
    from_scrape_dict,
)

logger = logging.getLogger(__name__)

GUBA_HOMEPAGE_URL = "https://guba.eastmoney.com/"
GUBA_ORIGIN = "https://guba.eastmoney.com"

# Detail URL pattern from probe doc: /news,{stock_code},{post_id}.html
DETAIL_URL_RE = re.compile(r"/news,\d+,\d+\.html$")

# Inline body image CDN — per probe, guba bodies host pictures here.
INLINE_IMAGE_HOST_RE = re.compile(r"gbres\.dfcfw\.com/Files/picture/", re.IGNORECASE)

# Selector for the rendered feed container on the homepage.
MAINLIST_SELECTOR = "#mainlist"
# Card anchor selector inside the mainlist — we deliberately scope to links
# whose href matches the detail URL pattern so we ignore nav/sidebar links.
CARD_ANCHOR_SELECTOR = "#mainlist a[href*='/news,']"

# Detail body container — primary selector first, fallbacks after.
BODY_SELECTORS: tuple[str, ...] = (
    ".article-body",
    "[class*='article-content']",
    "#zwconbody",
    "article",
)

# Author header candidates on the detail page.
AUTHOR_SELECTORS: tuple[str, ...] = (
    ".article-meta .author",
    ".author-name",
    ".user-name",
    ".pub_info .name",
)

PAGE_TIMEOUT_MS = 20000
MAINLIST_TIMEOUT_MS = 10000
DETAIL_HTTP_TIMEOUT = 10.0


class EastmoneyGubaAdapter:
    """Adapter for East Money guba homepage 精选 feed.

    Implements ``observers.base.SourceAdapter``.
    """

    name = "eastmoney_guba"
    cookie_env_key = ""  # no auth required (public pages)
    rate_limit_per_hour = 6

    def __init__(
        self,
        homepage_url: str = GUBA_HOMEPAGE_URL,
        min_content_length: int = 3000,
        max_posts_per_fetch: int = 15,
        detail_concurrency: int = 3,
        tier_default: int = 0,
        page_timeout_ms: int = PAGE_TIMEOUT_MS,
        mainlist_timeout_ms: int = MAINLIST_TIMEOUT_MS,
        detail_http_timeout: float = DETAIL_HTTP_TIMEOUT,
    ) -> None:
        self._homepage_url = homepage_url
        self._min_content_length = int(min_content_length)
        self._max_posts = int(max_posts_per_fetch)
        self._detail_concurrency = int(detail_concurrency)
        self._tier_default = tier_default
        self._page_timeout_ms = page_timeout_ms
        self._mainlist_timeout_ms = mainlist_timeout_ms
        self._detail_http_timeout = detail_http_timeout

    # ------------------------------------------------------------------
    # SourceAdapter surface
    # ------------------------------------------------------------------

    async def fetch_latest(self, since: datetime) -> list[Observation]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        try:
            cards = await self._fetch_feed_cards()
        except Exception as exc:  # noqa: BLE001
            logger.warning("eastmoney_guba homepage fetch failed: %s", exc)
            return []
        if not cards:
            return []

        cards = cards[: self._max_posts]
        sem = asyncio.Semaphore(self._detail_concurrency)

        async def _enrich(card: dict[str, Any]) -> dict[str, Any] | None:
            async with sem:
                try:
                    html = await self._fetch_detail_html(card["detail_url"])
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "guba detail fetch failed %s: %s", card["detail_url"], exc
                    )
                    return None
            parsed = self._parse_detail_html(html, card["detail_url"])
            if parsed is None:
                return None
            return {**card, **parsed}

        enriched = await asyncio.gather(*(_enrich(c) for c in cards))

        observations: list[Observation] = []
        for raw in enriched:
            if raw is None:
                continue
            try:
                obs = from_scrape_dict(
                    raw, source=self.name, tier=self._tier_default  # type: ignore[arg-type]
                )
            except ObservationValidationError as exc:
                logger.debug("guba skip raw: %s", exc)
                continue
            if obs.posted_at <= since:
                # Detail page rarely exposes a true timestamp; we fall back to
                # ``datetime.now`` in the parser, so this branch is mostly a
                # safety net.
                pass
            observations.append(obs)
        return observations

    async def health_check(self) -> bool:
        try:
            cards = await self._fetch_feed_cards()
            return isinstance(cards, list)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # browser + http internals (mocked in tests)
    # ------------------------------------------------------------------

    async def _fetch_feed_cards(self) -> list[dict[str, Any]]:
        """Load the homepage in headless Chromium and pull card stubs.

        Returns dicts with ``title`` + ``detail_url``. Tests should monkeypatch
        this method so unit runs never launch a browser.
        """
        from playwright.async_api import async_playwright

        cards: list[dict[str, Any]] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context()
                page = await ctx.new_page()
                # wait_until='domcontentloaded' — guba homepage's "load" event
                # never settles (analytics/trackers keep pinging). We just need
                # the HTML; the #mainlist wait below handles React hydration.
                await page.goto(
                    self._homepage_url,
                    timeout=self._page_timeout_ms,
                    wait_until="domcontentloaded",
                )
                try:
                    await page.wait_for_selector(
                        f"{MAINLIST_SELECTOR} a[href*='/news,']",
                        timeout=self._mainlist_timeout_ms,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.info(
                        "guba homepage mainlist did not populate: %s", exc
                    )
                    return []

                anchors = page.locator(CARD_ANCHOR_SELECTOR)
                count = await anchors.count()
                seen_urls: set[str] = set()
                for idx in range(count):
                    anchor = anchors.nth(idx)
                    try:
                        href = (await anchor.get_attribute("href")) or ""
                        title = ((await anchor.inner_text()) or "").strip()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("guba skip card %d: %s", idx, exc)
                        continue
                    detail_url = self._normalise_detail_url(href)
                    if not detail_url or not title:
                        continue
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    cards.append({"title": title, "detail_url": detail_url})
            finally:
                await browser.close()
        return cards

    async def _fetch_detail_html(self, url: str) -> str:
        """GET a detail page over httpx. SSR per probe — no JS needed."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": GUBA_HOMEPAGE_URL,
        }
        async with httpx.AsyncClient(
            timeout=self._detail_http_timeout, follow_redirects=True
        ) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text

    # ------------------------------------------------------------------
    # Pure parsing — easy to unit-test
    # ------------------------------------------------------------------

    def _parse_detail_html(
        self, html: str, detail_url: str
    ) -> dict[str, Any] | None:
        """Extract body text + first inline image + author from a detail page.

        Returns ``None`` if the body is shorter than ``min_content_length`` or
        if no inline body image is present.
        """
        soup = BeautifulSoup(html, "html.parser")

        body_node = None
        for sel in BODY_SELECTORS:
            body_node = soup.select_one(sel)
            if body_node is not None:
                break
        if body_node is None:
            return None

        # Body text: strip HTML, keep visible text only. Chinese chars count
        # as 1 because Python str length operates on code points.
        body_text = body_node.get_text(separator="\n", strip=True)
        if len(body_text) < self._min_content_length:
            return None

        # Inline image MUST come from inside the body, NOT from page-level
        # og:image / thumbnail. Prefer the CDN pattern but fall back to any
        # in-body <img> with an absolute URL.
        image_url = ""
        for img in body_node.find_all("img"):
            src = (img.get("src") or img.get("data-src") or "").strip()
            if not src:
                continue
            if src.startswith("//"):
                src = "https:" + src
            if not src.startswith("http"):
                continue
            if INLINE_IMAGE_HOST_RE.search(src):
                image_url = src
                break
            if not image_url:
                # Remember the first absolute image as fallback; keep scanning
                # for a preferred CDN match.
                image_url = src
        if not image_url:
            return None

        author = self._extract_author(soup)

        return {
            "author_handle": author or "guba_user",
            "content": body_text[:8000],
            "image_url": image_url,
            "has_image": True,
            "posted_at": datetime.now(timezone.utc),
            "raw_url": detail_url,
            "likes": 0,
            "retweets": 0,
            "replies": 0,
        }

    @staticmethod
    def _extract_author(soup: BeautifulSoup) -> str:
        for sel in AUTHOR_SELECTORS:
            node = soup.select_one(sel)
            if node is None:
                continue
            text = node.get_text(strip=True)
            if text:
                return text[:60]
        return ""

    @staticmethod
    def _normalise_detail_url(href: str) -> str:
        if not href:
            return ""
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("/"):
            href = GUBA_ORIGIN + href
        if not href.startswith("http"):
            return ""
        # Strip query/fragment for matching, keep canonical form.
        path = href.split("?")[0].split("#")[0]
        if not DETAIL_URL_RE.search(path):
            return ""
        return path
