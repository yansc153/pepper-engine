"""East Money 股吧 (guba) hot-thread adapter.

Scrapes multiple per-stock forums from guba.eastmoney.com using Playwright.
The list page (``/list,<code>,99.html``) ranks threads by read count; for each
thread above the configured threshold we visit the detail page to extract the
body text + first image. Threads without an image (or with < 100 chars of body)
are dropped, because the writer treats this as a "rewritable original" source.

Tier 0 — topic/content source only, NEVER enters the learning corpus. The
miner filters by ``author_tier > 0`` so these rows never get distilled.

Tests mock ``_fetch_list_page`` and ``_fetch_detail_page`` so unit runs never
spin a real browser.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from observers.base import (
    Observation,
    ObservationValidationError,
    from_scrape_dict,
)

logger = logging.getLogger(__name__)

GUBA_LIST_URL_TEMPLATE = "https://guba.eastmoney.com/list,{code},99.html"

# CSS selector candidates probed against the live page (2026-05-18).
# guba ships several layout variants (legacy + new "min-htbk"); we try in
# order and use the first that returns rows.
LIST_ROW_SELECTORS: tuple[str, ...] = (
    "table.default_list tbody tr",
    ".gb-list .gb-item",
    ".articleh",
    ".min-htbk-list .min-htbk-item",
    "div.tab_content_li",
)

# Number conversion: "1.2万" / "12345" / "999"
_TEN_K_RE = re.compile(r"^([\d.]+)\s*万$")

PAGE_TIMEOUT_MS = 15000
DETAIL_TIMEOUT_MS = 5000
DETAIL_CONCURRENCY = 3
MIN_CONTENT_LENGTH = 100


def _parse_count(raw: str) -> int:
    """Parse '1.2万' / '12345' / '' into an int. Returns 0 on failure."""
    text = (raw or "").strip().replace(",", "")
    if not text:
        return 0
    m = _TEN_K_RE.match(text)
    if m:
        try:
            return int(float(m.group(1)) * 10_000)
        except ValueError:
            return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


class EastmoneyGubaAdapter:
    """Adapter for East Money's per-stock forums (股吧).

    Implements ``observers.base.SourceAdapter``.
    """

    name = "eastmoney_guba"
    cookie_env_key = ""  # no auth required (public pages)
    rate_limit_per_hour = 6

    def __init__(
        self,
        stock_codes: list[str],
        min_reads: int = 10_000,
        tier_default: int = 0,
        max_posts_per_fetch: int = 20,
        page_timeout_ms: int = PAGE_TIMEOUT_MS,
        detail_timeout_ms: int = DETAIL_TIMEOUT_MS,
        detail_concurrency: int = DETAIL_CONCURRENCY,
    ) -> None:
        if not stock_codes:
            raise ValueError("stock_codes must be non-empty")
        self._stock_codes = list(stock_codes)
        self._min_reads = int(min_reads)
        self._tier_default = tier_default
        self._max_posts = max_posts_per_fetch
        self._page_timeout_ms = page_timeout_ms
        self._detail_timeout_ms = detail_timeout_ms
        self._detail_concurrency = detail_concurrency

    # ------------------------------------------------------------------
    # SourceAdapter surface
    # ------------------------------------------------------------------

    def list_url_for(self, code: str) -> str:
        return GUBA_LIST_URL_TEMPLATE.format(code=code)

    async def fetch_latest(self, since: datetime) -> list[Observation]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

        from playwright.async_api import async_playwright

        observations: list[Observation] = []
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    ctx = await browser.new_context()
                    observations = await self._scrape_all_forums(ctx, since)
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("eastmoney_guba fetch failed: %s", exc)
            return []
        return observations[: self._max_posts]

    async def health_check(self) -> bool:
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    ctx = await browser.new_context()
                    page = await ctx.new_page()
                    await page.goto(
                        self.list_url_for(self._stock_codes[0]),
                        timeout=self._page_timeout_ms,
                    )
                    return True
                finally:
                    await browser.close()
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _scrape_all_forums(
        self, ctx: Any, since: datetime
    ) -> list[Observation]:
        all_candidates: list[dict[str, Any]] = []
        for code in self._stock_codes:
            try:
                rows = await self._fetch_list_page(ctx, code)
            except Exception as exc:  # noqa: BLE001
                logger.warning("guba list fetch failed for %s: %s", code, exc)
                continue
            # Filter by reads on the cheap (list-page) data first.
            survivors = [r for r in rows if r.get("reads", 0) >= self._min_reads]
            all_candidates.extend(survivors)

        # Cap the detail-fetch fan-out so a popular index doesn't drown out
        # the others; respect max_posts but allow some headroom for image drops.
        all_candidates = all_candidates[: self._max_posts * 2]

        sem = asyncio.Semaphore(self._detail_concurrency)

        async def _enrich(row: dict[str, Any]) -> dict[str, Any] | None:
            async with sem:
                try:
                    detail = await self._fetch_detail_page(ctx, row["detail_url"])
                except Exception as exc:  # noqa: BLE001
                    logger.debug("guba detail fetch failed %s: %s", row["detail_url"], exc)
                    return None
            merged = {**row, **detail}
            return merged

        enriched = await asyncio.gather(*(_enrich(r) for r in all_candidates))

        observations: list[Observation] = []
        for raw in enriched:
            if raw is None:
                continue
            if not raw.get("image_url"):
                continue
            if len((raw.get("content") or "").strip()) < MIN_CONTENT_LENGTH:
                continue
            try:
                obs = from_scrape_dict(
                    raw, source=self.name, tier=self._tier_default  # type: ignore[arg-type]
                )
            except ObservationValidationError as exc:
                logger.debug("guba skip raw: %s", exc)
                continue
            if obs.posted_at <= since:
                continue
            observations.append(obs)
        return observations

    async def _fetch_list_page(
        self, ctx: Any, stock_code: str
    ) -> list[dict[str, Any]]:
        """Open the list page for one stock and parse thread rows.

        Returns a list of dicts with: title, author_handle, reads, comments,
        detail_url. Body + image are filled in later via _fetch_detail_page.
        """
        page = await ctx.new_page()
        try:
            await page.goto(
                self.list_url_for(stock_code), timeout=self._page_timeout_ms
            )
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:  # noqa: BLE001
                pass

            rows_locator = None
            for sel in LIST_ROW_SELECTORS:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    rows_locator = loc
                    break
            if rows_locator is None:
                logger.info("guba %s: no list rows matched any selector", stock_code)
                return []

            results: list[dict[str, Any]] = []
            count = await rows_locator.count()
            for idx in range(count):
                row = rows_locator.nth(idx)
                try:
                    parsed = await self._extract_list_row(row)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("guba %s skip row %d: %s", stock_code, idx, exc)
                    continue
                if parsed:
                    results.append(parsed)
            return results
        finally:
            await page.close()

    @staticmethod
    async def _extract_list_row(row: Any) -> dict[str, Any] | None:
        """Parse one thread row from the list page.

        guba's list table puts read/comment counts in the first two columns
        and the title (with href) further along. The exact column index has
        drifted between layouts so we use inner_text + a link probe.
        """
        text = (await row.inner_text()).strip()
        if not text:
            return None
        # Cells are tab-separated in the table layout, whitespace in the new
        # "min-htbk" layout. Normalise to a list of non-empty tokens.
        cells = [c.strip() for c in re.split(r"[\t\n]+", text) if c.strip()]
        if len(cells) < 3:
            return None

        # Heuristic: first numeric cell = reads, second numeric cell = comments.
        numeric_cells = [c for c in cells if re.match(r"^[\d.]+\s*万?$", c)]
        reads = _parse_count(numeric_cells[0]) if numeric_cells else 0
        comments = _parse_count(numeric_cells[1]) if len(numeric_cells) > 1 else 0

        # Title link
        link = row.locator("a").first
        if await link.count() == 0:
            return None
        href = (await link.get_attribute("href")) or ""
        title = ((await link.inner_text()) or "").strip()
        if not href or not title:
            return None
        if href.startswith("/"):
            detail_url = "https://guba.eastmoney.com" + href
        elif href.startswith("http"):
            detail_url = href
        else:
            detail_url = "https://guba.eastmoney.com/" + href.lstrip("/")

        # Author tends to be the last non-numeric, non-title cell.
        author = ""
        for cell in reversed(cells):
            if cell and cell != title and not re.match(r"^[\d.]+\s*万?$", cell):
                # Skip date-like cells (MM-DD HH:MM)
                if re.match(r"^\d{1,2}-\d{1,2}", cell):
                    continue
                author = cell[:60]
                break

        return {
            "title": title,
            "author_handle": author or "guba_user",
            "reads": reads,
            "comments": comments,
            "detail_url": detail_url,
        }

    async def _fetch_detail_page(
        self, ctx: Any, detail_url: str
    ) -> dict[str, Any]:
        """Visit a thread detail page and pull body + first image + timestamp."""
        page = await ctx.new_page()
        try:
            await page.goto(detail_url, timeout=self._detail_timeout_ms)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:  # noqa: BLE001
                pass

            # Body candidates — newzwcontent is the modern container; legacy
            # threads use stockcodec or zwconttbn.
            body = ""
            for sel in (
                "#zwconbody",
                ".newstext",
                ".article-body",
                "#zw_body",
                ".stockcodec",
            ):
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    body = ((await loc.inner_text()) or "").strip()
                    if body:
                        break

            # First in-article image
            image_url = ""
            for sel in (
                "#zwconbody img",
                ".newstext img",
                ".article-body img",
                "#zw_body img",
                ".stockcodec img",
            ):
                img = page.locator(sel).first
                if await img.count() > 0:
                    src = (await img.get_attribute("src")) or ""
                    if src.startswith("//"):
                        src = "https:" + src
                    if src.startswith("http"):
                        image_url = src
                        break

            # Timestamp — guba renders "2026-05-18 09:30:00" in a .time span
            posted_at: datetime = datetime.now(timezone.utc)
            time_loc = page.locator(".time, .pub_time, .zwfbtime").first
            if await time_loc.count() > 0:
                ts_text = ((await time_loc.inner_text()) or "").strip()
                m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(:\d{2})?)", ts_text)
                if m:
                    raw = m.group(1)
                    fmt = "%Y-%m-%d %H:%M:%S" if m.group(2) else "%Y-%m-%d %H:%M"
                    try:
                        naive = datetime.strptime(raw, fmt)
                        posted_at = naive.replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass

            return {
                "content": body[:5000],
                "image_url": image_url,
                "has_image": bool(image_url),
                "posted_at": posted_at,
                "raw_url": detail_url,
                "likes": 0,
                "retweets": 0,
                "replies": 0,
            }
        finally:
            await page.close()
