"""Unit tests for Benzinga adapter — HTML parsing is mocked."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from observers.base import SourceAdapter  # noqa: E402
from observers.benzinga_adapter import BenzingaAdapter  # noqa: E402


def test_implements_source_adapter_protocol() -> None:
    adapter = BenzingaAdapter(tickers=["AAPL"])
    assert isinstance(adapter, SourceAdapter)
    assert adapter.name == "benzinga"
    assert adapter.cookie_env_key == ""
    assert adapter.rate_limit_per_hour == 6


def test_init_rejects_empty_tickers() -> None:
    with pytest.raises(ValueError, match="tickers must be non-empty"):
        BenzingaAdapter(tickers=[])


def test_init_uppercases_tickers() -> None:
    adapter = BenzingaAdapter(tickers=["aapl", "Msft"])
    assert adapter._tickers == ["AAPL", "MSFT"]


def test_extract_article_paths_finds_news_links() -> None:
    html = (
        '<html><body>'
        '<a href="/news/24/01/foo-news">Foo</a>'
        '<a href="/trading-ideas/bar-idea">Bar</a>'
        '<a href="/analyst-ratings/baz">Baz</a>'
        '<a href="/news/24/01/foo-news">Duplicate</a>'
        '<a href="/some-other-section/x">Skip</a>'
        '</body></html>'
    )
    paths = BenzingaAdapter._extract_article_paths(html)
    assert "/news/24/01/foo-news" in paths
    assert "/trading-ideas/bar-idea" in paths
    assert "/analyst-ratings/baz" in paths
    # de-dup
    assert paths.count("/news/24/01/foo-news") == 1
    # only article sections
    assert "/some-other-section/x" not in paths


def test_parse_article_html_extracts_title_body_image() -> None:
    adapter = BenzingaAdapter(tickers=["AAPL"])
    html = '''<html><head>
        <meta property="og:title" content="Apple Beats Earnings | Benzinga" />
        <meta property="og:image" content="https://cdn.benzinga.com/files/aapl.jpg" />
    </head><body>
        <article class="article-content">
        <p>Apple reported strong quarterly results today with revenue up 12% year-over-year.</p>
        <p>Analysts are revising their price targets upward, citing strength in services revenue.</p>
        <p>The stock jumped 5% in after-hours trading on the news.</p>
        <p>Looking ahead, the company guided to continued momentum in iPhone sales.</p>
        </article>
    </body></html>'''
    result = adapter._parse_article_html(html, "https://www.benzinga.com/news/foo", "AAPL")
    assert result is not None
    assert result["author_handle"].startswith("benzinga:") or "AAPL" in result["author_handle"] or len(result["author_handle"]) > 0
    assert "Apple Beats Earnings" in result["content"]
    assert "revenue up 12%" in result["content"]
    assert "| Benzinga" not in result["content"]  # tail stripped
    assert result["image_url"] == "https://cdn.benzinga.com/files/aapl.jpg"
    assert result["has_image"] is True


def test_parse_article_html_drops_when_below_min_length() -> None:
    adapter = BenzingaAdapter(tickers=["AAPL"], min_content_length=500)
    html = '''<html><head>
        <meta property="og:title" content="Short" />
        <meta property="og:image" content="https://x/y.jpg" />
    </head><body><article class="article-content">tiny</article></body></html>'''
    assert adapter._parse_article_html(html, "https://www.benzinga.com/news/x", "AAPL") is None


def test_parse_article_html_drops_when_no_image() -> None:
    adapter = BenzingaAdapter(tickers=["AAPL"])
    html = '''<html><head>
        <meta property="og:title" content="Long article with no image attached" />
    </head><body><article class="article-content">''' + ("word " * 200) + '''</article></body></html>'''
    assert adapter._parse_article_html(html, "https://www.benzinga.com/news/x", "AAPL") is None
