"""Tests for src/observers/news_flash_adapter.py.

HTTP is mocked by monkeypatching the private ``_fetch_payload`` method.
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
from observers.news_flash_adapter import NewsFlashAdapter  # noqa: E402

FIXTURE = (
    PROJECT_ROOT / "tests" / "fixtures" / "observations" / "news_flash_20260101.json"
)


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_implements_source_adapter_protocol() -> None:
    adapter = NewsFlashAdapter()
    assert isinstance(adapter, SourceAdapter)
    assert adapter.name == "news_flash"
    assert adapter.cookie_env_key == ""
    assert adapter.rate_limit_per_hour == 30


@pytest.mark.asyncio
async def test_fetch_latest_parses_payload(fixture_payload, monkeypatch) -> None:
    adapter = NewsFlashAdapter()

    async def _fake(self):
        return fixture_payload

    monkeypatch.setattr(NewsFlashAdapter, "_fetch_payload", _fake)
    obs_list = await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert len(obs_list) == 5
    assert all(o.source == "news_flash" for o in obs_list)
    assert all(o.author_tier == 0 for o in obs_list)
    assert all(o.author_handle == "eastmoney_kuaixun" for o in obs_list)


@pytest.mark.asyncio
async def test_fetch_latest_filters_by_since(fixture_payload, monkeypatch) -> None:
    adapter = NewsFlashAdapter()

    async def _fake(self):
        return fixture_payload

    monkeypatch.setattr(NewsFlashAdapter, "_fetch_payload", _fake)
    future = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert await adapter.fetch_latest(future) == []


@pytest.mark.asyncio
async def test_fetch_latest_handles_500(monkeypatch) -> None:
    adapter = NewsFlashAdapter()

    async def _boom(self):
        raise httpx.HTTPError("500")

    monkeypatch.setattr(NewsFlashAdapter, "_fetch_payload", _boom)
    assert (
        await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc)) == []
    )


@pytest.mark.asyncio
async def test_health_check_true_on_200(monkeypatch) -> None:
    adapter = NewsFlashAdapter()

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *args, **kwargs): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, *args, **kwargs): return _FakeResp()

    monkeypatch.setattr("observers.news_flash_adapter.httpx.AsyncClient", _FakeClient)
    assert await adapter.health_check() is True


@pytest.mark.asyncio
async def test_health_check_false_on_network_error(monkeypatch) -> None:
    adapter = NewsFlashAdapter()

    class _BadClient:
        def __init__(self, *args, **kwargs): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, *args, **kwargs):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr("observers.news_flash_adapter.httpx.AsyncClient", _BadClient)
    assert await adapter.health_check() is False


@pytest.mark.asyncio
async def test_fetch_latest_skips_rows_missing_url(monkeypatch) -> None:
    payload = {"data": {"list": [{"title": "no url", "showtime": "2026-01-01 09:00:00"}]}}
    adapter = NewsFlashAdapter()

    async def _fake(self):
        return payload

    monkeypatch.setattr(NewsFlashAdapter, "_fetch_payload", _fake)
    obs_list = await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert obs_list == []
