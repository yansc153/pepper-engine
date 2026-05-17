"""
Logged-in market content scraper.
Primary source: Xueqiu community feed from the dedicated Chrome profile.
KOL scraping is still handled by TwitterBot.scrape_kol_posts().
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import BrowserContext, async_playwright

from config import (
    CHROME_CDP_URL,
    IMAGE_CACHE_DIR,
    MAX_IMAGE_SIZE_MB,
    SOURCE_USER_AGENT,
    XUEQIU_HOME_URL,
)

logger = logging.getLogger(__name__)


@dataclass
class ScrapedItem:
    title: str
    url: str
    source: str
    snippet: str = ""
    summary: str = ""
    category: str = ""
    content_type: str = ""
    image_url: str = ""
    published_at: str = ""
    engagement: dict = field(default_factory=dict)
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class KOLPost:
    handle: str
    tier: str
    post_url: str
    content: str
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    posted_at: str = ""
    is_viral: bool = False


def classify_market_item(title: str, summary: str, source: str) -> str:
    del source
    text = f"{title} {summary}".lower()
    if any(word in text for word in ("财报", "业绩", "guidance", "earnings", "ebit", "净利", "eps")):
        return "earnings_reaction"
    if any(word in text for word in ("轮动", "板块", "rotation", "sector", "资金", "supply chain", "订单")):
        return "sector_rotation"
    if any(word in text for word in ("止盈", "止损", "仓位", "回撤", "持仓", "情绪", "纪律", "加仓", "减仓")):
        return "trading_psychology"
    return "controversy" if any(word in text for word in ("争议", "分歧", "警告", "暴跌", "崩", "风险")) else "market_hot_take"


async def _http_get_text(url: str) -> str | None:
    cmd = [
        "curl", "-s", "-f", "-L",
        "--max-time", "15",
        "-H", f"User-Agent: {SOURCE_USER_AGENT}",
        url,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        if process.returncode != 0:
            return None
        return stdout.decode("utf-8", errors="replace")
    except Exception:
        return None


async def download_image(image_url: str, filename: str = "") -> str | None:
    if not image_url:
        return None
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not filename:
        import hashlib

        url_hash = hashlib.md5(image_url.encode()).hexdigest()[:12]
        ext = ".jpg"
        if ".png" in image_url.lower():
            ext = ".png"
        elif ".webp" in image_url.lower():
            ext = ".webp"
        filename = f"img_{url_hash}{ext}"
    output_path = IMAGE_CACHE_DIR / filename
    cmd = [
        "curl", "-s", "-f", "-L",
        "--max-time", "30",
        "--max-filesize", str(MAX_IMAGE_SIZE_MB * 1024 * 1024),
        "-o", str(output_path),
        image_url,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(process.communicate(), timeout=35)
        if process.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1024:
            return str(output_path)
        output_path.unlink(missing_ok=True)
        return None
    except Exception:
        output_path.unlink(missing_ok=True)
        return None


async def fetch_image_for_item(item: ScrapedItem) -> str | None:
    image_url = item.image_url
    if image_url:
        return await download_image(image_url)
    return None


async def _connected_context() -> tuple[object, object, BrowserContext]:
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect_over_cdp(CHROME_CDP_URL)
    if not browser.contexts:
        await browser.close()
        await playwright.stop()
        raise RuntimeError(f"No Chrome contexts found at {CHROME_CDP_URL}")
    return playwright, browser, browser.contexts[0]


def _first_post_url(hrefs: list[str], base_url: str, pattern: str) -> str:
    for href in hrefs:
        if re.match(pattern, href):
            return urljoin(base_url, href)
    return ""


def _clean_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line in {"关注", "推荐", "查看更多評論...", "查看更多评论..."}:
            continue
        lines.append(line)
    return lines


def _pick_content_image(urls: list[str]) -> str:
    for raw in urls:
        if not raw:
            continue
        url = raw.strip()
        lowered = url.lower()
        if not url.startswith("http"):
            continue
        if any(bad in lowered for bad in ("avatar", "emoji", "emoticon", "icon", "badge", "logo")):
            continue
        if any(host in lowered for host in ("xqimg", "xueqiu", "staticimg", "gbres")):
            return url
    return ""


async def _refresh_xueqiu_expert_feed(page) -> None:
    """
    Xueqiu expert feed does not always refresh on plain reload.
    Force a tab switch, then click "达人" again so new posts are pulled in.
    """
    await page.goto(XUEQIU_HOME_URL, wait_until="domcontentloaded", timeout=20000)
    await page.wait_for_timeout(3000)

    popular_tab = page.get_by_text("热门", exact=True)
    expert_tab = page.get_by_text("达人", exact=True)

    if await popular_tab.count():
        await popular_tab.first.click()
        await page.wait_for_timeout(1200)

    if await expert_tab.count():
        await expert_tab.first.click()
        await page.wait_for_timeout(3000)
    else:
        # Fallback: one more reload, then retry expert tab once.
        await page.reload(wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)
        if await expert_tab.count():
            await expert_tab.first.click()
            await page.wait_for_timeout(3000)


async def scrape_xueqiu_items(context: BrowserContext, limit: int = 10) -> list[ScrapedItem]:
    page = await context.new_page()
    items: list[ScrapedItem] = []
    try:
        await _refresh_xueqiu_expert_feed(page)
        # Scroll down a few times to load more posts
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 1200)")
            await page.wait_for_timeout(1500)
        cards = page.locator("article")
        count = await cards.count()
        for idx in range(count):
            if len(items) >= limit:
                break
            card = cards.nth(idx)
            raw_text = (await card.inner_text()).strip()
            lines = _clean_lines(raw_text)
            if len(lines) < 2:
                continue
            hrefs = await card.locator("a").evaluate_all("(els) => els.map((el) => el.getAttribute('href') || '')")
            image_urls = await card.locator("img").evaluate_all(
                "(els) => els.map((el) => el.getAttribute('src') || el.getAttribute('data-src') || el.currentSrc || '')"
            )
            post_url = _first_post_url(hrefs, XUEQIU_HOME_URL, r"^/\d+/\d+$")
            if not post_url:
                continue
            image_url = _pick_content_image(image_urls)
            title = lines[1] if len(lines) > 1 else lines[0]
            summary = "\n".join(lines[1:5])[:800]
            items.append(
                ScrapedItem(
                    title=title,
                    url=post_url,
                    source="雪球达人",
                    snippet=title,
                    summary=summary,
                    category="social_market",
                    content_type=classify_market_item(title, summary, "雪球达人"),
                    image_url=image_url,
                )
            )
    finally:
        await page.close()
    logger.info("Xueqiu expert feed: scraped %d items", len(items))
    return items


async def scrape_all_news() -> list[ScrapedItem]:
    playwright = browser = context = None
    try:
        playwright, browser, context = await _connected_context()
        xueqiu_items = await scrape_xueqiu_items(context)
    except Exception as exc:
        logger.error("Market scrape failed: %s", exc)
        return []
    finally:
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()

    deduped: list[ScrapedItem] = []
    seen_urls: set[str] = set()
    for item in xueqiu_items:
        if not item.url or item.url in seen_urls:
            continue
        seen_urls.add(item.url)
        deduped.append(item)
    logger.info(
        "Total scraped: %d items (%d Xueqiu, %d after dedup)",
        len(xueqiu_items),
        len(deduped),
    )
    return deduped


def dict_to_kol_post(d: dict) -> KOLPost:
    return KOLPost(
        handle=d.get("handle", ""),
        tier=d.get("tier", "tier3"),
        post_url=d.get("post_url", ""),
        content=d.get("content", ""),
        likes=d.get("likes", 0),
        retweets=d.get("retweets", 0),
        replies=d.get("replies", 0),
        is_viral=d.get("is_viral", False),
    )
