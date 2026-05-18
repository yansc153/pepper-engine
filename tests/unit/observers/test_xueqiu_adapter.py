"""Tests for src/observers/xueqiu_adapter.py.

HTTP is mocked by monkeypatching the private ``_fetch_payload`` method, which
keeps the tests independent from respx/httpx wiring quirks under Python 3.14.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from observers.base import SourceAdapter  # noqa: E402
from observers.xueqiu_adapter import XueqiuAdapter  # noqa: E402

FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "observations" / "xueqiu_hot_20260101.json"


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def cookie_env(tmp_path, monkeypatch):
    cookies = [{"name": "cookiesu", "value": "abc123", "domain": ".xueqiu.com"}]
    cookie_path = tmp_path / "xueqiu_cookies.json"
    cookie_path.write_text(json.dumps(cookies), encoding="utf-8")
    monkeypatch.setenv("XUEQIU_COOKIE_FILE", str(cookie_path))
    return cookie_path


def test_implements_source_adapter_protocol() -> None:
    adapter = XueqiuAdapter()
    assert isinstance(adapter, SourceAdapter)
    assert adapter.name == "xueqiu"
    assert adapter.cookie_env_key == "XUEQIU_COOKIE_FILE"
    assert adapter.rate_limit_per_hour == 24


@pytest.mark.asyncio
async def test_fetch_latest_parses_payload(cookie_env, fixture_payload, monkeypatch) -> None:
    adapter = XueqiuAdapter()

    async def _fake(self):
        return fixture_payload

    monkeypatch.setattr(XueqiuAdapter, "_fetch_payload", _fake)
    obs_list = await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc))
    # parser now falls back to 'xueqiu_topic' handle for headline items that
    # lack a user, so empty-handle rows are kept (5 total). They must still
    # have content + target — the fixture's empty-handle row has both.
    assert len(obs_list) == 5
    assert all(o.source == "xueqiu" for o in obs_list)
    assert all(o.author_tier == 2 for o in obs_list)
    assert any(o.has_image for o in obs_list)
    assert all(o.raw_url.startswith("https://xueqiu.com/") for o in obs_list)


@pytest.mark.asyncio
async def test_fetch_latest_filters_by_since(cookie_env, fixture_payload, monkeypatch) -> None:
    adapter = XueqiuAdapter()

    async def _fake(self):
        return fixture_payload

    monkeypatch.setattr(XueqiuAdapter, "_fetch_payload", _fake)
    future = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert await adapter.fetch_latest(future) == []


@pytest.mark.asyncio
async def test_fetch_latest_swallows_http_errors(cookie_env, monkeypatch) -> None:
    adapter = XueqiuAdapter()

    async def _boom(self):
        raise httpx.HTTPError("500")

    monkeypatch.setattr(XueqiuAdapter, "_fetch_payload", _boom)
    obs_list = await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert obs_list == []


@pytest.mark.asyncio
async def test_fetch_latest_missing_cookie_file_returns_empty(monkeypatch) -> None:
    monkeypatch.setenv("XUEQIU_COOKIE_FILE", "/nonexistent/path.json")
    adapter = XueqiuAdapter()
    obs_list = await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert obs_list == []


@pytest.mark.asyncio
async def test_health_check_false_without_cookies(monkeypatch) -> None:
    monkeypatch.delenv("XUEQIU_COOKIE_FILE", raising=False)
    adapter = XueqiuAdapter()
    assert await adapter.health_check() is False


@pytest.mark.asyncio
async def test_parse_payload_skips_rows_without_content_or_target(cookie_env, monkeypatch) -> None:
    """Empty handle is no longer fatal (we fall back to 'xueqiu_topic'),
    but rows missing BOTH content AND target must still be dropped."""
    bad_payload = {"list": [
        {"user": {"screen_name": ""}, "text": "", "target": "",
         "created_at": 1767225600000, "fav_count": 1, "retweet_count": 0, "reply_count": 0},
    ]}

    async def _fake(self):
        return bad_payload

    monkeypatch.setattr(XueqiuAdapter, "_fetch_payload", _fake)
    adapter = XueqiuAdapter()
    obs_list = await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert obs_list == []
