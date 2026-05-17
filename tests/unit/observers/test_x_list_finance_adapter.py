"""Tests for src/observers/x_list_finance_adapter.py.

Patches ``_fetch_via_twitter_bot`` so we never spin Playwright.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from observers.base import SourceAdapter  # noqa: E402
from observers.x_list_finance_adapter import XListFinanceAdapter  # noqa: E402


def _raw(handle: str, text: str, when: datetime, **extra) -> dict:
    base = {
        "handle": handle,
        "text": text,
        "created_at": when.isoformat(),
        "likes": 10,
        "retweets": 1,
        "replies": 2,
        "views": 100,
        "has_media": False,
        "url": f"https://x.com/{handle.lstrip('@')}/status/1",
    }
    base.update(extra)
    return base


def test_implements_source_adapter_protocol() -> None:
    adapter = XListFinanceAdapter()
    assert isinstance(adapter, SourceAdapter)
    assert adapter.name == "x_list_finance"
    assert adapter.cookie_env_key == "TWITTER_COOKIE_FILE"
    assert adapter.rate_limit_per_hour == 12


@pytest.mark.asyncio
async def test_fetch_latest_parses_and_tags_tier(monkeypatch) -> None:
    adapter = XListFinanceAdapter()
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    fixture = [
        _raw("@alpha", "盘前快评", now, likes=200, has_media=True),
        _raw("@beta", "板块轮动", now - timedelta(minutes=5)),
    ]

    async def _fake(self):
        return fixture

    monkeypatch.setattr(XListFinanceAdapter, "_fetch_via_twitter_bot", _fake)
    obs = await adapter.fetch_latest(now - timedelta(hours=1))
    assert len(obs) == 2
    assert all(o.source == "x_list_finance" for o in obs)
    assert all(o.author_tier == 1 for o in obs)
    assert obs[0].author_handle == "alpha"  # @ stripped
    assert obs[0].has_image is True


@pytest.mark.asyncio
async def test_fetch_latest_filters_by_since(monkeypatch) -> None:
    adapter = XListFinanceAdapter()
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    fixture = [_raw("@alpha", "stale", now - timedelta(days=2))]

    async def _fake(self):
        return fixture

    monkeypatch.setattr(XListFinanceAdapter, "_fetch_via_twitter_bot", _fake)
    assert await adapter.fetch_latest(now - timedelta(hours=1)) == []


@pytest.mark.asyncio
async def test_fetch_latest_swallows_bot_errors(monkeypatch) -> None:
    adapter = XListFinanceAdapter()

    async def _boom(self):
        raise RuntimeError("playwright down")

    monkeypatch.setattr(XListFinanceAdapter, "_fetch_via_twitter_bot", _boom)
    assert await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc)) == []


@pytest.mark.asyncio
async def test_fetch_latest_skips_malformed_rows(monkeypatch) -> None:
    adapter = XListFinanceAdapter()
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    fixture = [
        {"handle": "@x", "text": "bad"},  # missing created_at
        _raw("@good", "ok", now),
    ]

    async def _fake(self):
        return fixture

    monkeypatch.setattr(XListFinanceAdapter, "_fetch_via_twitter_bot", _fake)
    obs = await adapter.fetch_latest(now - timedelta(hours=1))
    assert len(obs) == 1
    assert obs[0].author_handle == "good"


@pytest.mark.asyncio
async def test_health_check_true_when_fetch_returns_list(monkeypatch) -> None:
    adapter = XListFinanceAdapter()

    async def _fake(self):
        return []

    monkeypatch.setattr(XListFinanceAdapter, "_fetch_via_twitter_bot", _fake)
    assert await adapter.health_check() is True


@pytest.mark.asyncio
async def test_health_check_false_on_exception(monkeypatch) -> None:
    adapter = XListFinanceAdapter()

    async def _boom(self):
        raise RuntimeError("not logged in")

    monkeypatch.setattr(XListFinanceAdapter, "_fetch_via_twitter_bot", _boom)
    assert await adapter.health_check() is False


def test_list_url_constructor_override() -> None:
    custom = "https://x.com/i/lists/999"
    a = XListFinanceAdapter(list_url=custom, max_posts_per_fetch=5)
    assert a._list_url == custom
    assert a._max_posts == 5
