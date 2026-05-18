"""Benzinga US-stock article adapter — httpx-based.

Per-ticker quote pages on Benzinga (https://www.benzinga.com/quote/<SYMBOL>)
list recent articles about that stock: analyst commentary, market moves,
news takes — "second-level analysis" content. Each article page has a
full body (1-3k words typically) + cover image.

Two-stage scrape:
  1. Per-ticker quote page: extract article URLs + titles
  2. For each article: GET the article page, parse out title/body/image
Both stages use simple httpx (no Playwright); Benzinga renders article
shells server-side and there's no anti-bot challenge from the VPS IP.

Tier 0 — topic/content source only, NEVER enters the learning corpus.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from observers.base import (
    Observation,
    ObservationValidationError,
    from_scrape_dict,
)

logger = logging.getLogger(__name__)

BENZINGA_QUOTE_URL_TEMPLATE = "https://www.benzinga.com/quote/{ticker}"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Regex to find Benzinga article URLs on a quote page. Articles live under
# /news/, /trading-ideas/, /analyst-ratings/, /movers/, etc. The URL slug
# uniquely identifies an article; we de-dup on it.
_ARTICLE_LINK_RE = re.compile(
    r'href="(/(?:news|trading-ideas|analyst-ratings|markets|movers|long-ideas|short-ideas|government|education|earnings|insider-trades)/[a-z0-9\-/]+)"',
    re.IGNORECASE,
)

# Article body content lives in a wrapper. Try multiple class names since
# Benzinga has refreshed their layout a few times.
_BODY_HTML_RE = re.compile(
    r'<(?:div|article)[^>]*class="[^"]*(?:article-content|article-body|entry-content|post-content|story-body)[^"]*"[^>]*>(.*?)</(?:div|article)>',
    re.IGNORECASE | re.DOTALL,
)

# Fallback: look for og:description meta tag
_OG_DESCRIPTION_RE = re.compile(
    r'<meta\s+property="og:description"\s+content="([^"]+)"', re.IGNORECASE
)

# Image: prefer og:image (cover), fall back to first <img> inside body
_OG_IMAGE_RE = re.compile(
    r'<meta\s+property="og:image"\s+content="([^"]+)"', re.IGNORECASE
)
_INLINE_IMG_RE = re.compile(r'<img[^>]+src="(https?://[^"]+)"', re.IGNORECASE)

# Title: prefer og:title, fall back to <title>
_OG_TITLE_RE = re.compile(
    r'<meta\s+property="og:title"\s+content="([^"]+)"', re.IGNORECASE
)
_TITLE_TAG_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Author from byline / meta
_BYLINE_RE = re.compile(
    r'(?:by\s+|author[^>]*>)\s*<[^>]+>\s*([A-Za-z][A-Za-z\s\.\']{2,40})',
    re.IGNORECASE,
)

# Strip leftover tags + collapse whitespace when serializing body
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

PAGE_TIMEOUT = 15.0
DETAIL_TIMEOUT = 10.0
DETAIL_CONCURRENCY = 3
MIN_CONTENT_LENGTH = 300  # English articles, words bigger than Chinese chars


class BenzingaAdapter:
    """Adapter for Benzinga US-stock article pages.

    Implements ``observers.base.SourceAdapter``.
    """

    name = "benzinga"
    cookie_env_key = ""  # no auth required (public pages)
    rate_limit_per_hour = 6

    def __init__(
        self,
        tickers: list[str],
        tier_default: int = 0,
        max_posts_per_fetch: int = 15,
        min_content_length: int = MIN_CONTENT_LENGTH,
        detail_concurrency: int = DETAIL_CONCURRENCY,
    ) -> None:
        if not tickers:
            raise ValueError("tickers must be non-empty")
        self._tickers = [t.upper() for t in tickers]
        self._tier_default = tier_default
        self._max_posts = max_posts_per_fetch
        self._min_content_length = min_content_length
        self._detail_concurrency = detail_concurrency

    # ------------------------------------------------------------------
    # SourceAdapter surface
    # ------------------------------------------------------------------

    async def fetch_latest(self, since: datetime) -> list[Observation]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        try:
            articles = await self._scrape_all_tickers()
        except Exception as exc:  # noqa: BLE001
            logger.warning("benzinga fetch failed: %s", exc)
            return []
        return self._build_observations(articles, since)

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=PAGE_TIMEOUT) as client:
                resp = await client.get(
                    BENZINGA_QUOTE_URL_TEMPLATE.format(ticker="AAPL"),
                    headers={"User-Agent": DEFAULT_USER_AGENT},
                )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _scrape_all_tickers(self) -> list[dict[str, Any]]:
        """Collect article dicts across all configured tickers, de-duped by url."""
        async with httpx.AsyncClient(
            timeout=PAGE_TIMEOUT,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            follow_redirects=True,
        ) as client:
            # Stage 1: per-ticker quote pages → list of article paths
            article_paths: dict[str, str] = {}  # path → ticker (for de-dup)
            for ticker in self._tickers:
                try:
                    quote_url = BENZINGA_QUOTE_URL_TEMPLATE.format(ticker=ticker)
                    resp = await client.get(quote_url)
                    if resp.status_code != 200:
                        logger.debug("benzinga quote %s: %d", ticker, resp.status_code)
                        continue
                    for path in self._extract_article_paths(resp.text):
                        article_paths.setdefault(path, ticker)
                        if len(article_paths) >= self._max_posts:
                            break
                except Exception as exc:  # noqa: BLE001
                    logger.debug("benzinga quote %s failed: %s", ticker, exc)
                    continue
                if len(article_paths) >= self._max_posts:
                    break

            if not article_paths:
                logger.info("benzinga: 0 article paths found across %d tickers", len(self._tickers))
                return []

            logger.info(
                "benzinga: collected %d article paths across tickers, hydrating",
                len(article_paths),
            )

            # Stage 2: GET each article page, parse body+image+title+author
            sem = asyncio.Semaphore(self._detail_concurrency)

            async def _hydrate(path: str, ticker: str) -> dict[str, Any] | None:
                async with sem:
                    try:
                        full_url = f"https://www.benzinga.com{path}"
                        r = await client.get(full_url, timeout=DETAIL_TIMEOUT)
                        if r.status_code != 200:
                            logger.debug("benzinga detail %s: %d", path, r.status_code)
                            return None
                        return self._parse_article_html(r.text, full_url, ticker)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("benzinga detail %s failed: %s", path, exc)
                        return None

            results = await asyncio.gather(
                *[_hydrate(p, t) for p, t in article_paths.items()]
            )
            articles = [a for a in results if a]
            logger.info("benzinga: hydrated %d/%d articles", len(articles), len(article_paths))
            return articles

    @staticmethod
    def _extract_article_paths(html: str) -> list[str]:
        """Pull /news/... /trading-ideas/... etc. paths from a quote page."""
        seen: set[str] = set()
        out: list[str] = []
        for match in _ARTICLE_LINK_RE.finditer(html):
            path = match.group(1)
            # Drop fragments + query strings
            path = path.split("#", 1)[0].split("?", 1)[0]
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
        return out

    def _parse_article_html(
        self, html: str, full_url: str, ticker: str
    ) -> dict[str, Any] | None:
        # Title
        m = _OG_TITLE_RE.search(html)
        title = m.group(1).strip() if m else ""
        if not title:
            m = _TITLE_TAG_RE.search(html)
            title = (m.group(1).strip() if m else "")[:200]
        # Strip "| Benzinga" tail
        title = re.sub(r"\s*\|\s*Benzinga.*$", "", title, flags=re.IGNORECASE).strip()

        # Body
        body_match = _BODY_HTML_RE.search(html)
        body_html = body_match.group(1) if body_match else ""
        body_text = _TAG_RE.sub(" ", body_html).strip()
        body_text = _WHITESPACE_RE.sub(" ", body_text)
        if not body_text:
            # Fallback to og:description (short but usable)
            d = _OG_DESCRIPTION_RE.search(html)
            body_text = d.group(1).strip() if d else ""

        content = f"{title}\n\n{body_text}".strip()
        if len(content) < self._min_content_length:
            return None

        # Image
        m = _OG_IMAGE_RE.search(html)
        image_url = m.group(1).strip() if m else ""
        if not image_url and body_html:
            inline = _INLINE_IMG_RE.search(body_html)
            image_url = inline.group(1).strip() if inline else ""
        if not image_url:
            return None  # image required

        # Author (best-effort)
        m = _BYLINE_RE.search(html)
        author = (m.group(1).strip() if m else f"benzinga:{ticker}")[:60]

        return {
            "author_handle": author,
            "content": content,
            "raw_url": full_url,
            "image_url": image_url,
            "has_image": True,
            "posted_at": datetime.now(timezone.utc),
            "likes": 0,
            "retweets": 0,
            "replies": 0,
        }

    def _build_observations(
        self, articles: list[dict[str, Any]], since: datetime
    ) -> list[Observation]:
        out: list[Observation] = []
        for raw in articles:
            try:
                obs = from_scrape_dict(
                    raw, source=self.name, tier=self._tier_default  # type: ignore[arg-type]
                )
            except ObservationValidationError as exc:
                logger.debug("benzinga skip article: %s", exc)
                continue
            if obs.posted_at <= since:
                continue
            out.append(obs)
        return out
