"""Unit tests for src/publisher.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import publisher
from publisher import (
    PostResult,
    _content_hash,
    is_duplicate_recent,
    post_tweet,
    split_trailing_url,
)


# ── URL splitter ──────────────────────────────────────────────────────────


def test_split_trailing_url_none():
    assert split_trailing_url("纯文本无链接") == ("纯文本无链接", None)


def test_split_trailing_url_strips_last_url():
    main, url = split_trailing_url("特斯拉财报炸了 https://example.com/article")
    assert main == "特斯拉财报炸了"
    assert url == "https://example.com/article"


def test_split_trailing_url_strips_trailing_punctuation():
    main, url = split_trailing_url("快看 https://t.co/abc.")
    assert main == "快看"
    assert url == "https://t.co/abc"


def test_split_trailing_url_url_only_body_keeps_inline():
    # If the text is just a URL, don't produce an empty main post.
    text = "https://example.com/article"
    assert split_trailing_url(text) == (text, None)


# ── content hash + dedup ──────────────────────────────────────────────────


def test_content_hash_stable():
    assert _content_hash("同样的话") == _content_hash("同样的话")
    assert _content_hash("a") != _content_hash("b")


def test_is_duplicate_recent_true_when_db_returns_row(monkeypatch):
    fake_conn = MagicMock()
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.execute = MagicMock(
        return_value=MagicMock(fetchone=MagicMock(return_value=(1,)))
    )
    monkeypatch.setattr(publisher, "get_conn", lambda: fake_conn)

    assert is_duplicate_recent("hello") is True


def test_is_duplicate_recent_false_when_no_row(monkeypatch):
    fake_conn = MagicMock()
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.execute = MagicMock(
        return_value=MagicMock(fetchone=MagicMock(return_value=None))
    )
    monkeypatch.setattr(publisher, "get_conn", lambda: fake_conn)

    assert is_duplicate_recent("hello") is False


def test_is_duplicate_recent_swallows_db_errors(monkeypatch):
    def boom():
        raise RuntimeError("db locked")
    monkeypatch.setattr(publisher, "get_conn", boom)
    assert is_duplicate_recent("hello") is False


# ── post_tweet ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_tweet_dry_run_skips_bot(monkeypatch):
    monkeypatch.setattr(publisher, "is_duplicate_recent", lambda *a, **kw: False)
    # If the bot were ever constructed/used this would explode.
    monkeypatch.setattr(
        publisher, "TwitterBot",
        lambda *a, **kw: pytest.fail("DRY_RUN should not instantiate TwitterBot"),
    )
    result = await post_tweet("hello", dry_run=True)
    assert result == PostResult(success=True, tweet_url=None, error=None)


@pytest.mark.asyncio
async def test_post_tweet_dedup_blocks(monkeypatch):
    monkeypatch.setattr(publisher, "is_duplicate_recent", lambda *a, **kw: True)
    result = await post_tweet("hi", dry_run=False, bot=MagicMock())
    assert result.success is False
    assert "duplicate" in result.error


def _make_bot(post_result: dict, reply_result: dict | None = None) -> MagicMock:
    bot = MagicMock()
    bot.start = AsyncMock()
    bot.stop = AsyncMock()
    bot.ensure_logged_in = AsyncMock()
    bot.post_tweet = AsyncMock(return_value=post_result)
    bot.reply_to_tweet = AsyncMock(
        return_value=reply_result or {"success": True, "tweet_url": None, "error": None}
    )
    return bot


@pytest.mark.asyncio
async def test_post_tweet_happy_path_no_url(monkeypatch):
    monkeypatch.setattr(publisher, "is_duplicate_recent", lambda *a, **kw: False)
    bot = _make_bot({"success": True, "tweet_url": "https://x.com/u/status/1", "error": None})

    result = await post_tweet("无链接正文", dry_run=False, bot=bot)
    assert result.success is True
    assert result.tweet_url == "https://x.com/u/status/1"
    bot.reply_to_tweet.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_tweet_with_url_triggers_reply(monkeypatch):
    monkeypatch.setattr(publisher, "is_duplicate_recent", lambda *a, **kw: False)
    bot = _make_bot({"success": True, "tweet_url": "https://x.com/u/status/2", "error": None})

    result = await post_tweet(
        "特斯拉财报炸了 https://example.com/article",
        dry_run=False,
        bot=bot,
    )

    assert result.success is True
    # main post text was stripped of URL
    main_call_text = bot.post_tweet.await_args.args[0]
    assert "https://example.com" not in main_call_text
    bot.reply_to_tweet.assert_awaited_once()
    reply_call = bot.reply_to_tweet.await_args
    assert reply_call.args[0] == "https://x.com/u/status/2"
    assert reply_call.args[1] == "https://example.com/article"


@pytest.mark.asyncio
async def test_post_tweet_bubbles_bot_error(monkeypatch):
    monkeypatch.setattr(publisher, "is_duplicate_recent", lambda *a, **kw: False)
    bot = _make_bot({"success": False, "tweet_url": None, "error": "post button not found"})
    result = await post_tweet("hi", dry_run=False, bot=bot)
    assert result.success is False
    assert result.error == "post button not found"


@pytest.mark.asyncio
async def test_post_tweet_not_logged_in_returned_as_error(monkeypatch):
    from twitter_bot import NotLoggedInError
    monkeypatch.setattr(publisher, "is_duplicate_recent", lambda *a, **kw: False)
    bot = _make_bot({"success": True, "tweet_url": "x", "error": None})
    bot.ensure_logged_in = AsyncMock(side_effect=NotLoggedInError("cookie expired"))

    result = await post_tweet("hi", dry_run=False, bot=bot)
    assert result.success is False
    assert "not_logged_in" in result.error


@pytest.mark.asyncio
async def test_post_tweet_reply_failure_does_not_fail_main(monkeypatch, caplog):
    monkeypatch.setattr(publisher, "is_duplicate_recent", lambda *a, **kw: False)
    bot = _make_bot(
        {"success": True, "tweet_url": "https://x.com/u/status/3", "error": None},
        reply_result={"success": False, "tweet_url": None, "error": "rate limit"},
    )
    result = await post_tweet(
        "看这篇 https://example.com/x", dry_run=False, bot=bot,
    )
    assert result.success is True
    assert result.tweet_url == "https://x.com/u/status/3"


@pytest.mark.asyncio
async def test_post_tweet_env_dry_run(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(
        publisher, "TwitterBot",
        lambda *a, **kw: pytest.fail("env DRY_RUN should bypass bot"),
    )
    result = await post_tweet("hi")
    assert result.success is True
    assert result.tweet_url is None
