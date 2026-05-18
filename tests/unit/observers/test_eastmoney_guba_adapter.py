"""Tests for src/observers/eastmoney_guba_adapter.py.

The browser-side path (_fetch_feed_cards) and the network-side path
(_fetch_detail_html) are both monkeypatched so unit runs never spin
Playwright or hit the live network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from observers.base import SourceAdapter  # noqa: E402
from observers.eastmoney_guba_adapter import EastmoneyGubaAdapter  # noqa: E402


# Build a body with ≥3000 Chinese chars. Use a repeating sentence so the
# resulting text easily clears the threshold but stays human-readable.
_LONG_PARAGRAPH = "今天市场情绪偏谨慎，盘面分歧加大，主线轮动加快。" * 200


def _detail_html(body_html: str, author: str = "测试作者") -> str:
    return f"""<!doctype html>
<html><head>
  <meta property="og:image" content="https://gbres.dfcfw.com/Files/picture/THUMB_DO_NOT_USE.jpg">
</head><body>
  <div class="article-meta"><span class="author">{author}</span></div>
  <div class="article-body">{body_html}</div>
</body></html>"""


def test_implements_source_adapter_protocol() -> None:
    adapter = EastmoneyGubaAdapter()
    assert isinstance(adapter, SourceAdapter)
    assert adapter.name == "eastmoney_guba"
    assert adapter.cookie_env_key == ""
    assert adapter.rate_limit_per_hour == 6


def test_init_defaults() -> None:
    adapter = EastmoneyGubaAdapter()
    assert adapter._homepage_url == "https://guba.eastmoney.com/"
    assert adapter._min_content_length == 3000
    assert adapter._max_posts == 15
    assert adapter._detail_concurrency == 3
    assert adapter._tier_default == 0


def test_parse_detail_html_extracts_body_and_image() -> None:
    adapter = EastmoneyGubaAdapter()
    html = _detail_html(
        f"<p>{_LONG_PARAGRAPH}</p>"
        "<img src='https://gbres.dfcfw.com/Files/picture/20260518/AAA_w1366h767.jpg'>"
        "<p>更多分析略。</p>"
    )
    url = "https://guba.eastmoney.com/news,600111,1709939844.html"

    parsed = adapter._parse_detail_html(html, url)

    assert parsed is not None
    assert parsed["raw_url"] == url
    assert parsed["has_image"] is True
    assert parsed["image_url"] == (
        "https://gbres.dfcfw.com/Files/picture/20260518/AAA_w1366h767.jpg"
    )
    # The og:image must NOT leak in.
    assert "THUMB_DO_NOT_USE" not in parsed["image_url"]
    assert parsed["author_handle"] == "测试作者"
    assert len(parsed["content"]) >= 3000


def test_parse_detail_html_drops_short_body() -> None:
    adapter = EastmoneyGubaAdapter()
    short_html = _detail_html(
        "<p>就这么几个字。</p>"
        "<img src='https://gbres.dfcfw.com/Files/picture/20260518/X.jpg'>"
    )

    assert (
        adapter._parse_detail_html(
            short_html, "https://guba.eastmoney.com/news,1,2.html"
        )
        is None
    )


def test_parse_detail_html_drops_no_inline_image() -> None:
    adapter = EastmoneyGubaAdapter()
    html = _detail_html(f"<p>{_LONG_PARAGRAPH}</p>")  # no <img>

    assert (
        adapter._parse_detail_html(
            html, "https://guba.eastmoney.com/news,1,2.html"
        )
        is None
    )


@pytest.mark.asyncio
async def test_fetch_via_browser_handles_empty_feed_gracefully(
    monkeypatch,
) -> None:
    adapter = EastmoneyGubaAdapter()

    async def _empty_feed(self):
        return []

    async def _should_not_be_called(self, url):  # pragma: no cover - guard
        raise AssertionError("detail fetch must not run on empty feed")

    monkeypatch.setattr(
        EastmoneyGubaAdapter, "_fetch_feed_cards", _empty_feed
    )
    monkeypatch.setattr(
        EastmoneyGubaAdapter, "_fetch_detail_html", _should_not_be_called
    )

    obs = await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert obs == []


@pytest.mark.asyncio
async def test_fetch_latest_end_to_end_with_mocks(monkeypatch) -> None:
    """Sanity check: cards → detail HTML → Observation list."""
    adapter = EastmoneyGubaAdapter()

    cards = [
        {
            "title": "长文一",
            "detail_url": "https://guba.eastmoney.com/news,600111,1.html",
        },
        {
            "title": "短文二（应被丢弃）",
            "detail_url": "https://guba.eastmoney.com/news,600111,2.html",
        },
    ]

    long_html = _detail_html(
        f"<p>{_LONG_PARAGRAPH}</p>"
        "<img src='https://gbres.dfcfw.com/Files/picture/20260518/OK.jpg'>"
    )
    short_html = _detail_html("<p>太短。</p>")

    async def _fake_cards(self):
        return cards

    async def _fake_detail(self, url):
        return long_html if url.endswith(",1.html") else short_html

    monkeypatch.setattr(EastmoneyGubaAdapter, "_fetch_feed_cards", _fake_cards)
    monkeypatch.setattr(EastmoneyGubaAdapter, "_fetch_detail_html", _fake_detail)

    obs = await adapter.fetch_latest(datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert len(obs) == 1
    only = obs[0]
    assert only.source == "eastmoney_guba"
    assert only.author_tier == 0
    assert only.has_image is True
    assert only.image_url and "gbres.dfcfw.com" in only.image_url
    assert only.raw_url.endswith(",1.html")
