"""
Twitter/X browser automation via Playwright headless Chromium.

This is the content_2 fork of the 花椒创业板 twitter_bot, adapted for the
content_2 deployment model (VPS Docker, headless, cookie file injection).
The CDP / connect_over_cdp / DOM-injection black magic from the dev-mac
flow is intentionally removed — headless Playwright's ``set_input_files``
handles image uploads directly without the ``window.name`` bridge.

Public surface used by S5b (x_list_adapter), S10 (reviewer), S13 (Discord
publisher_callback) and S7 publisher.py:

  - async ensure_logged_in() -> None       (raises NotLoggedInError)
  - async post_tweet(text, image_path)     -> dict
  - async reply_to_tweet(tweet_url, text, image_path=None) -> dict
  - async scrape_list_by_url(list_url, max_posts=30) -> list[dict]
  - async get_post_metrics(tweet_url) -> dict

Cookie file path comes from the ``TWITTER_COOKIE_FILE`` env var. In prod
the file lives at ``/app/secrets/x_dahao_cookies.json``; the main account
cookie has NOT been provided yet (2026-05-18) — the code path is wired
for whenever it lands.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)


logger = logging.getLogger(__name__)

TWITTER_URL = "https://x.com"
TWITTER_HOME = "https://x.com/home"
COMPOSE_URL = "https://x.com/compose/post"

_ALLOWED_IMAGE_DIRS = (
    str(Path("/app/tmp_images").resolve()),
    str(Path(__file__).resolve().parent.parent / "tmp_images"),
)


def _validate_image_path(path: str) -> None:
    """Raise ValueError if path is outside the allowed tmp_images directory."""
    resolved = str(Path(path).resolve())
    if not any(resolved.startswith(d) for d in _ALLOWED_IMAGE_DIRS):
        raise ValueError(
            f"image_path '{path}' is outside allowed directories — "
            "possible LLM path traversal"
        )

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class NotLoggedInError(RuntimeError):
    """Raised when the cookie file is missing/expired or X redirects to login."""


class TwitterBot:
    """Headless Playwright wrapper around X.com."""

    SELECTORS: dict[str, str] = {
        "tweet_input": '[data-testid="tweetTextarea_0"]',
        "tweet_button": '[data-testid="tweetButtonInline"]',
        "reply_button": '[data-testid="tweetButton"]',
        "file_input": 'input[data-testid="fileInput"], input[type="file"][accept*="image"]',
        "tweet_article": 'article[data-testid="tweet"]',
        "tweet_text": '[data-testid="tweetText"]',
        "toast": '[data-testid="toast"]',
    }

    def __init__(
        self,
        cookie_file: str | os.PathLike[str] | None = None,
        headless: bool | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._cookie_file = Path(
            cookie_file
            or os.environ.get("TWITTER_COOKIE_FILE")
            or "/app/secrets/x_dahao_cookies.json"
        )
        if headless is None:
            headless = os.environ.get("BROWSER_HEADLESS", "1") != "0"
        self._headless = headless
        self._user_agent = user_agent or _DEFAULT_USER_AGENT

        self._playwright: Any | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch headless Chromium, load cookies, open a page."""
        if not self._cookie_file.exists():
            raise NotLoggedInError(
                f"Twitter cookie file missing: {self._cookie_file}"
            )

        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=self._user_agent,
        )

        try:
            cookies = json.loads(self._cookie_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise NotLoggedInError(
                f"Cookie file unreadable ({self._cookie_file}): {exc}"
            ) from exc
        await self.context.add_cookies(cookies)
        self.page = await self.context.new_page()
        logger.info(
            "TwitterBot started (headless=%s, cookies=%d)",
            self._headless,
            len(cookies),
        )

    async def stop(self) -> None:
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self.page = None
        self.context = None
        self.browser = None
        self._playwright = None

    async def __aenter__(self) -> "TwitterBot":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ── auth ───────────────────────────────────────────────────────────────

    async def ensure_logged_in(self) -> None:
        """
        Navigate to /home and verify the compose textarea is reachable.
        Raises NotLoggedInError on redirect to /login or missing element.
        """
        if self.page is None:
            await self.start()
        assert self.page is not None
        try:
            await self.page.goto(TWITTER_HOME, wait_until="load", timeout=20000)
        except Exception as exc:
            raise NotLoggedInError(f"home navigation failed: {exc}") from exc

        url = self.page.url
        if "/login" in url or "/i/flow/login" in url:
            raise NotLoggedInError(f"redirected to login: {url}")

        try:
            await self.page.wait_for_selector(
                self.SELECTORS["tweet_input"], timeout=15000
            )
        except Exception as exc:
            raise NotLoggedInError(
                "compose textarea not visible on /home — cookie likely expired"
            ) from exc

    # ── posting ────────────────────────────────────────────────────────────

    async def post_tweet(
        self, text: str, image_path: str | None = None
    ) -> dict[str, Any]:
        """
        Compose + send a tweet. Returns:
          {"success": bool, "tweet_url": str | None, "error": str | None}
        """
        if self.page is None:
            return {"success": False, "tweet_url": None, "error": "bot not started"}
        try:
            await self.page.goto(COMPOSE_URL, wait_until="load", timeout=20000)
            compose = await self.page.wait_for_selector(
                self.SELECTORS["tweet_input"], timeout=10000
            )
            if compose is None:
                return {
                    "success": False,
                    "tweet_url": None,
                    "error": "compose textarea not found",
                }
            await compose.click()
            await self.page.keyboard.type(text, delay=25)

            if image_path:
                try:
                    _validate_image_path(image_path)
                except ValueError as exc:
                    return {"success": False, "tweet_url": None, "error": str(exc)}
                if not os.path.exists(image_path):
                    return {
                        "success": False,
                        "tweet_url": None,
                        "error": f"image not found: {image_path}",
                    }
                try:
                    await self.page.set_input_files(
                        self.SELECTORS["file_input"], image_path
                    )
                except Exception as exc:
                    return {
                        "success": False,
                        "tweet_url": None,
                        "error": f"image upload failed: {exc}",
                    }
                # Allow upload + preview render
                await self.page.wait_for_timeout(2500)

            post_btn = await self.page.wait_for_selector(
                self.SELECTORS["tweet_button"], timeout=8000
            )
            if post_btn is None:
                return {
                    "success": False,
                    "tweet_url": None,
                    "error": "post button not found",
                }
            await post_btn.click()

            tweet_url = await self._capture_tweet_url_from_toast()
            if tweet_url is None:
                return {
                    "success": False,
                    "tweet_url": None,
                    "error": "post toast not detected (tweet may or may not have sent)",
                }
            logger.info("tweet posted: %s", tweet_url)
            return {"success": True, "tweet_url": tweet_url, "error": None}

        except NotLoggedInError:
            raise
        except Exception as exc:
            logger.exception("post_tweet failed")
            return {"success": False, "tweet_url": None, "error": str(exc)}

    async def reply_to_tweet(
        self,
        tweet_url: str,
        text: str,
        image_path: str | None = None,
    ) -> dict[str, Any]:
        """Reply to ``tweet_url``. Same return shape as post_tweet."""
        if self.page is None:
            return {"success": False, "tweet_url": None, "error": "bot not started"}
        try:
            await self.page.goto(tweet_url, wait_until="load", timeout=20000)
            reply_input = await self.page.wait_for_selector(
                self.SELECTORS["tweet_input"], timeout=10000
            )
            if reply_input is None:
                return {
                    "success": False,
                    "tweet_url": None,
                    "error": "reply input not found",
                }
            await reply_input.click()
            await self.page.keyboard.type(text, delay=25)

            if image_path and os.path.exists(image_path):
                try:
                    _validate_image_path(image_path)
                    await self.page.set_input_files(
                        self.SELECTORS["file_input"], image_path
                    )
                    await self.page.wait_for_timeout(2500)
                except ValueError as exc:
                    logger.warning("reply image rejected: %s", exc)
                except Exception as exc:
                    logger.warning("reply image upload failed: %s", exc)

            btn = await self.page.wait_for_selector(
                self.SELECTORS["reply_button"], timeout=8000
            )
            if btn is None:
                return {
                    "success": False,
                    "tweet_url": None,
                    "error": "reply button not found",
                }
            await btn.click()
            reply_url = await self._capture_tweet_url_from_toast()
            return {
                "success": reply_url is not None,
                "tweet_url": reply_url,
                "error": None if reply_url else "reply toast not detected",
            }
        except Exception as exc:
            logger.exception("reply_to_tweet failed")
            return {"success": False, "tweet_url": None, "error": str(exc)}

    async def _capture_tweet_url_from_toast(self) -> str | None:
        """Read the /status/<id> link from X's post-confirmation toast."""
        if self.page is None:
            return None
        try:
            toast = await self.page.wait_for_selector(
                self.SELECTORS["toast"], timeout=10000
            )
            link = await toast.query_selector('a[href*="/status/"]')
            if link is None:
                return None
            href = await link.get_attribute("href")
            if not href:
                return None
            return f"https://x.com{href}" if href.startswith("/") else href
        except Exception as exc:
            logger.warning("toast capture failed: %s", exc)
            return None

    # ── scraping ───────────────────────────────────────────────────────────

    async def scrape_list_by_url(
        self, list_url: str, max_posts: int = 30
    ) -> list[dict[str, Any]]:
        """
        Scrape a Twitter List timeline. Returned dicts are intentionally
        shaped to feed ``observers.base.from_scrape_dict`` (handle/text/
        created_at/likes/retweets/replies/views/has_media/url).
        """
        if self.page is None:
            return []
        posts: list[dict[str, Any]] = []
        try:
            await self.page.goto(list_url, wait_until="load", timeout=20000)
            await self.page.wait_for_selector(
                self.SELECTORS["tweet_article"], timeout=10000
            )
            tweet_elements = await self.page.query_selector_all(
                self.SELECTORS["tweet_article"]
            )
            for tweet_el in tweet_elements[:max_posts]:
                text_el = await tweet_el.query_selector(self.SELECTORS["tweet_text"])
                if text_el is None:
                    continue
                content = await text_el.inner_text()

                handle = ""
                for link in await tweet_el.query_selector_all('a[role="link"]'):
                    href = await link.get_attribute("href") or ""
                    if href.startswith("/") and "/" not in href[1:] and len(href) > 2:
                        handle = f"@{href[1:]}"
                        break

                likes, retweets, replies, views = 0, 0, 0, 0
                for group in await tweet_el.query_selector_all('[role="group"] button'):
                    aria = (await group.get_attribute("aria-label") or "").lower()
                    if "like" in aria or "赞" in aria:
                        likes = _extract_number(aria)
                    elif "repost" in aria or "retweet" in aria:
                        retweets = _extract_number(aria)
                    elif "repl" in aria or "回复" in aria:
                        replies = _extract_number(aria)
                    elif "view" in aria or "浏览" in aria:
                        views = _extract_number(aria)

                tweet_perma = ""
                created_at = ""
                time_el = await tweet_el.query_selector("time")
                if time_el is not None:
                    perm = await time_el.evaluate("el => el.closest('a')?.href")
                    if perm:
                        tweet_perma = str(perm)
                    created_at = await time_el.get_attribute("datetime") or ""

                # Capture the first photo URL (twimg.com background image of
                # tweetPhoto). Skip if it's a video — we can't reuse video,
                # only photos can be re-attached to our own tweet.
                image_url = ""
                photo_el = await tweet_el.query_selector('[data-testid="tweetPhoto"] img')
                if photo_el is not None:
                    src = await photo_el.get_attribute("src")
                    if src and "twimg.com" in src:
                        image_url = src
                has_media = bool(image_url)

                posts.append(
                    {
                        "handle": handle,
                        "text": content,
                        "created_at": created_at,
                        "likes": likes,
                        "retweets": retweets,
                        "replies": replies,
                        "views": views,
                        "has_media": has_media,
                        "url": tweet_perma,
                        "image_url": image_url,
                    }
                )
        except Exception as exc:
            logger.error("scrape_list_by_url failed: %s", exc)
        return posts

    async def get_post_metrics(self, tweet_url: str) -> dict[str, int]:
        """Scrape engagement counters from a tweet permalink."""
        if self.page is None:
            return {}
        metrics = {"likes": 0, "retweets": 0, "replies": 0, "impressions": 0}
        try:
            await self.page.goto(tweet_url, wait_until="load", timeout=20000)
            for group in await self.page.query_selector_all('[role="group"] button'):
                aria = (await group.get_attribute("aria-label") or "").lower()
                if "like" in aria or "赞" in aria:
                    metrics["likes"] = _extract_number(aria)
                elif "repost" in aria or "retweet" in aria:
                    metrics["retweets"] = _extract_number(aria)
                elif "repl" in aria or "回复" in aria:
                    metrics["replies"] = _extract_number(aria)
                elif "view" in aria or "浏览" in aria:
                    metrics["impressions"] = _extract_number(aria)
        except Exception as exc:
            logger.error("get_post_metrics failed: %s", exc)
        return metrics


def _extract_number(text: str) -> int:
    """Parse '1.2K Likes' / '42 赞' / '3,401 Replies' → int."""
    match = re.search(r"([\d,.]+)\s*([KkMm])?", text)
    if not match:
        return 0
    try:
        num = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0
    suffix = (match.group(2) or "").upper()
    if suffix == "K":
        num *= 1_000
    elif suffix == "M":
        num *= 1_000_000
    return int(num)


# Async helper for ad-hoc throttling in batch flows
async def _jittered_sleep(low: float = 3.0, high: float = 8.0) -> None:
    await asyncio.sleep(random.uniform(low, high))
