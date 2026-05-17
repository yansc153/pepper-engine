"""Tests for src/observers/x_list_general_adapter.py.

The adapter is disabled by default (no list URL provided yet) — disabled
means ``fetch_latest`` returns ``[]`` without touching the network. When
``enabled=True`` and a URL is provided, behavior mirrors the finance variant.
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
from observers.x_list_general_adapter import XListGeneralAdapter  # noqa: E402


def test_implements_source_adapter_protocol() -> None:
    adapter = XListGeneralAdapter()
    assert isinstance(adapter, SourceAdapter)
    assert adapter.name == "x_list_general"
    assert adapter.cookie_env_key == "TWITTER_COOKIE_FILE"
    assert adapter.rate_limit_per_hour == 12


@pytest.mark.asyncio
async def test_disabled_fetch_returns_empty_without_network(monkeypatch) -> None:
    adapter = XListGeneralAdapter()  # enabled=False default

    async def _boom(self):  # must NOT be invoked
        raise AssertionError("_fetch_via_twitter_bot should be skipped when disabled")

    monkeypatch.setattr(XListGeneralAdapter, "_fetch_via_twitter_bot", _boom)
    assert await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc)) == []


@pytest.mark.asyncio
async def test_disabled_health_check_is_true() -> None:
    adapter = XListGeneralAdapter()
    # A disabled adapter shouldn't trip source_health alerts.
    assert await adapter.health_check() is True


@pytest.mark.asyncio
async def test_enabled_adapter_scrapes_and_tags_tier_3(monkeypatch) -> None:
    adapter = XListGeneralAdapter(
        list_url="https://x.com/i/lists/123", enabled=True
    )
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    fixture = [
        {
            "handle": "@meme",
            "text": "general lulz",
            "created_at": now.isoformat(),
            "likes": 5, "retweets": 0, "replies": 1,
            "has_media": True,
            "url": "https://x.com/meme/status/9",
        }
    ]

    async def _fake(self):
        return fixture

    monkeypatch.setattr(XListGeneralAdapter, "_fetch_via_twitter_bot", _fake)
    obs = await adapter.fetch_latest(now - timedelta(hours=1))
    assert len(obs) == 1
    assert obs[0].source == "x_list_general"
    assert obs[0].author_tier == 3


@pytest.mark.asyncio
async def test_enabled_swallows_errors(monkeypatch) -> None:
    adapter = XListGeneralAdapter(
        list_url="https://x.com/i/lists/123", enabled=True
    )

    async def _boom(self):
        raise RuntimeError("playwright down")

    monkeypatch.setattr(XListGeneralAdapter, "_fetch_via_twitter_bot", _boom)
    assert await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc)) == []
