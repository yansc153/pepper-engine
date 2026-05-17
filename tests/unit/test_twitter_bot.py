"""Unit tests for src/twitter_bot.py.

All Playwright calls are mocked via unittest.mock.AsyncMock. No real
network or browser launches happen here.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from twitter_bot import (
    NotLoggedInError,
    TwitterBot,
    _extract_number,
)


@pytest.fixture
def cookie_file(tmp_path: Path) -> Path:
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps([{"name": "auth_token", "value": "x", "domain": ".x.com"}]))
    return path


# ── pure helper ────────────────────────────────────────────────────────────


def test_extract_number_plain():
    assert _extract_number("42 Likes") == 42


def test_extract_number_k_suffix():
    assert _extract_number("1.2K Reposts") == 1200


def test_extract_number_m_suffix_and_commas():
    assert _extract_number("3,401,500 Views") == 3_401_500
    assert _extract_number("2.5M views") == 2_500_000


def test_extract_number_empty_returns_zero():
    assert _extract_number("no digits here") == 0
    assert _extract_number("") == 0


# ── lifecycle / login ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_raises_when_cookie_missing(tmp_path: Path):
    bot = TwitterBot(cookie_file=tmp_path / "nope.json")
    with pytest.raises(NotLoggedInError):
        await bot.start()


@pytest.mark.asyncio
async def test_ensure_logged_in_redirect_to_login_raises(cookie_file):
    """If page.url contains /login we must raise NotLoggedInError."""
    bot = TwitterBot(cookie_file=cookie_file)
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    type(page).url = "https://x.com/i/flow/login"
    bot.page = page

    with pytest.raises(NotLoggedInError):
        await bot.ensure_logged_in()


@pytest.mark.asyncio
async def test_ensure_logged_in_missing_compose_raises(cookie_file):
    bot = TwitterBot(cookie_file=cookie_file)
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock(side_effect=TimeoutError("no compose"))
    type(page).url = "https://x.com/home"
    bot.page = page

    with pytest.raises(NotLoggedInError):
        await bot.ensure_logged_in()


@pytest.mark.asyncio
async def test_ensure_logged_in_happy_path(cookie_file):
    bot = TwitterBot(cookie_file=cookie_file)
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock(return_value=MagicMock())
    type(page).url = "https://x.com/home"
    bot.page = page

    await bot.ensure_logged_in()
    page.goto.assert_awaited_once()
    page.wait_for_selector.assert_awaited_once()


# ── post_tweet ─────────────────────────────────────────────────────────────


def _make_page_for_post(toast_href: str | None = "/u/status/123") -> MagicMock:
    """Build a page mock that walks through post_tweet's happy path."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.type = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.set_input_files = AsyncMock()

    compose = MagicMock()
    compose.click = AsyncMock()
    post_btn = MagicMock()
    post_btn.click = AsyncMock()

    toast = MagicMock()
    link = MagicMock()
    link.get_attribute = AsyncMock(return_value=toast_href)
    toast.query_selector = AsyncMock(return_value=link if toast_href else None)

    async def _wait_for_selector(sel, timeout=0):
        if "tweetTextarea_0" in sel:
            return compose
        if "tweetButtonInline" in sel:
            return post_btn
        if "toast" in sel:
            return toast
        return None

    page.wait_for_selector = AsyncMock(side_effect=_wait_for_selector)
    return page


@pytest.mark.asyncio
async def test_post_tweet_success_no_image(cookie_file):
    bot = TwitterBot(cookie_file=cookie_file)
    bot.page = _make_page_for_post()

    result = await bot.post_tweet("hello world")
    assert result == {
        "success": True,
        "tweet_url": "https://x.com/u/status/123",
        "error": None,
    }


@pytest.mark.asyncio
async def test_post_tweet_image_missing_returns_error(cookie_file, tmp_path, monkeypatch):
    bot = TwitterBot(cookie_file=cookie_file)
    bot.page = _make_page_for_post()

    # Allow tmp_path under the validation whitelist
    import twitter_bot as tb
    monkeypatch.setattr(tb, "_ALLOWED_IMAGE_DIRS", (str(tmp_path.resolve()),))

    result = await bot.post_tweet("hello", image_path=str(tmp_path / "missing.jpg"))
    assert result["success"] is False
    assert "image not found" in result["error"]


@pytest.mark.asyncio
async def test_post_tweet_image_path_outside_allowed_dir_rejected(cookie_file):
    bot = TwitterBot(cookie_file=cookie_file)
    bot.page = _make_page_for_post()
    # /etc/passwd exists but is outside allowed image dirs → must reject
    result = await bot.post_tweet("hello", image_path="/etc/passwd")
    assert result["success"] is False
    assert "outside allowed directories" in result["error"]


@pytest.mark.asyncio
async def test_post_tweet_toast_missing_reports_uncertainty(cookie_file):
    bot = TwitterBot(cookie_file=cookie_file)
    bot.page = _make_page_for_post(toast_href=None)

    result = await bot.post_tweet("hello")
    assert result["success"] is False
    assert "toast" in result["error"]


@pytest.mark.asyncio
async def test_post_tweet_handles_unstarted_bot():
    bot = TwitterBot(cookie_file=Path("/tmp/anything"))
    # never called .start(), so .page is None
    result = await bot.post_tweet("hi")
    assert result == {"success": False, "tweet_url": None, "error": "bot not started"}


# ── scraping ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scrape_list_by_url_returns_empty_on_no_tweets(cookie_file):
    bot = TwitterBot(cookie_file=cookie_file)
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.query_selector_all = AsyncMock(return_value=[])
    bot.page = page

    posts = await bot.scrape_list_by_url("https://x.com/i/lists/1")
    assert posts == []
