"""Tests for src/observers/futu_adapter.py.

Mocks the browser path via monkeypatching ``_fetch_via_browser`` so we never
spin Playwright in unit tests.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from observers.base import SourceAdapter  # noqa: E402
from observers.futu_adapter import FutuAdapter  # noqa: E402

FIXTURE = (
    PROJECT_ROOT / "tests" / "fixtures" / "observations" / "futu_recommend_20260101.json"
)


@pytest.fixture
def fixture_posts() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def cookie_env(tmp_path, monkeypatch):
    cookies = [{"name": "session", "value": "xyz", "domain": ".futunn.com"}]
    cookie_path = tmp_path / "futu_cookies.json"
    cookie_path.write_text(json.dumps(cookies), encoding="utf-8")
    monkeypatch.setenv("FUTU_COOKIE_FILE", str(cookie_path))
    return cookie_path


def test_implements_source_adapter_protocol() -> None:
    adapter = FutuAdapter()
    assert isinstance(adapter, SourceAdapter)
    assert adapter.name == "futu"
    assert adapter.cookie_env_key == "FUTU_COOKIE_FILE"
    assert adapter.rate_limit_per_hour == 12


@pytest.mark.asyncio
async def test_fetch_latest_parses_browser_payload(
    cookie_env, fixture_posts, monkeypatch
) -> None:
    adapter = FutuAdapter()

    async def _fake_fetch(self):
        return fixture_posts

    monkeypatch.setattr(FutuAdapter, "_fetch_via_browser", _fake_fetch)
    obs_list = await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert len(obs_list) == 5
    assert all(o.source == "futu" for o in obs_list)
    assert all(o.author_tier == 0 for o in obs_list)  # tier=0: topic source, not learned
    # one fixture post has has_image=false
    assert sum(1 for o in obs_list if o.has_image) == 4


@pytest.mark.asyncio
async def test_fetch_latest_filters_by_since(
    cookie_env, fixture_posts, monkeypatch
) -> None:
    adapter = FutuAdapter()

    async def _fake_fetch(self):
        return fixture_posts

    monkeypatch.setattr(FutuAdapter, "_fetch_via_browser", _fake_fetch)
    future = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert await adapter.fetch_latest(future) == []


@pytest.mark.asyncio
async def test_fetch_latest_swallows_browser_errors(cookie_env, monkeypatch) -> None:
    adapter = FutuAdapter()

    async def _boom(self):
        raise RuntimeError("playwright exploded")

    monkeypatch.setattr(FutuAdapter, "_fetch_via_browser", _boom)
    obs_list = await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert obs_list == []


@pytest.mark.asyncio
async def test_health_check_false_when_cookies_missing(monkeypatch) -> None:
    monkeypatch.delenv("FUTU_COOKIE_FILE", raising=False)
    adapter = FutuAdapter()
    assert await adapter.health_check() is False


@pytest.mark.asyncio
async def test_health_check_true_when_fetch_returns_list(
    cookie_env, monkeypatch
) -> None:
    adapter = FutuAdapter()

    async def _fake_fetch(self):
        return [{"author_handle": "x", "content": "y", "raw_url": "u"}]

    monkeypatch.setattr(FutuAdapter, "_fetch_via_browser", _fake_fetch)
    assert await adapter.health_check() is True
