"""Tests for src/observers/self_monitor_adapter.py.

Uses a real sqlite tmp DB (cheap; migrations are fast) and stubs the
timeline scrape via the ``timeline_fetcher`` injection point — no Playwright,
no x.com.
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
from observers.self_monitor_adapter import (  # noqa: E402
    SelfMonitorAdapter,
    content_hash,
)
from src.database import get_conn, init_db  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "selfmon.db"
    init_db(p)
    return p


@pytest.fixture
def cookie_file(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "x_xiaohao_cookies.json"
    p.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("X_XIAOHAO_COOKIE_FILE", str(p))
    monkeypatch.setenv("TWITTER_HANDLE", "huajiao_test")
    return p


def _insert_draft(db_path: Path, content: str, *, tweet_url: str | None = None) -> int:
    conn = get_conn(db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO drafts (content, content_length, content_mode, "
                "optimal_length, topic_lane, persona, pattern_ids, "
                "source_observation_ids, tweet_url, status) "
                "VALUES (?, ?, 'insight', 'short', 'pre_market', "
                "'finance_neutral', '[]', '[]', ?, 'approved')",
                (content, len(content), tweet_url),
            )
            return int(cur.lastrowid)
    finally:
        conn.close()


def _now() -> datetime:
    return datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)


def _tweet(text: str, url: str, when: datetime) -> dict:
    return {"text": text, "url": url, "created_at": when.isoformat()}


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_implements_source_adapter_protocol(cookie_file) -> None:
    adapter = SelfMonitorAdapter()
    assert isinstance(adapter, SourceAdapter)
    assert adapter.name == "self_monitor"


@pytest.mark.asyncio
async def test_fetch_latest_always_empty(cookie_file) -> None:
    adapter = SelfMonitorAdapter()
    assert await adapter.fetch_latest(_now()) == []


@pytest.mark.asyncio
async def test_reconcile_raises_without_handle(monkeypatch, db_path) -> None:
    monkeypatch.delenv("TWITTER_HANDLE", raising=False)
    adapter = SelfMonitorAdapter()

    async def _empty():
        return []

    with pytest.raises(RuntimeError, match="TWITTER_HANDLE"):
        await adapter.reconcile(db_path=db_path, timeline_fetcher=_empty)


@pytest.mark.asyncio
async def test_reconcile_binds_unbound_draft(cookie_file, db_path) -> None:
    now = _now()
    text = "盘前快评 中概 反弹 注意 高位 锁仓"
    draft_id = _insert_draft(db_path, text)
    url = "https://x.com/huajiao_test/status/100"
    tweets = [_tweet(text, url, now - timedelta(hours=1))]

    adapter = SelfMonitorAdapter()

    async def _fake():
        return tweets

    result = await adapter.reconcile(
        db_path=db_path, timeline_fetcher=_fake, now=now
    )
    assert result.bound == 1
    assert result.wild == 0
    assert result.errors == 0

    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT tweet_url, cross_referenced, status FROM drafts WHERE id=?",
            (draft_id,),
        ).fetchone()
        assert row["tweet_url"] == url
        assert row["cross_referenced"] == 1
        assert row["status"] == "published"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_reconcile_skips_already_bound_draft(cookie_file, db_path) -> None:
    """If a draft already has tweet_url, the matching scraped tweet writes wild."""
    now = _now()
    text = "intraday 板块 轮动 观察"
    _insert_draft(db_path, text, tweet_url="https://x.com/huajiao_test/status/55")
    new_url = "https://x.com/huajiao_test/status/56"
    tweets = [_tweet(text, new_url, now - timedelta(hours=1))]

    adapter = SelfMonitorAdapter()

    async def _fake():
        return tweets

    result = await adapter.reconcile(
        db_path=db_path, timeline_fetcher=_fake, now=now
    )
    assert result.bound == 0
    assert result.wild == 1

    conn = get_conn(db_path)
    try:
        wild = conn.execute(
            "SELECT content, content_hash FROM wild_posts WHERE tweet_url=?",
            (new_url,),
        ).fetchone()
        assert wild["content"] == text
        assert wild["content_hash"] == content_hash(text)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_reconcile_records_wild_when_no_draft(cookie_file, db_path) -> None:
    """Manually-posted tweet (no matching draft) lands in wild_posts."""
    now = _now()
    text = "手动发的野生推文 不进学习库"
    url = "https://x.com/huajiao_test/status/200"
    tweets = [_tweet(text, url, now - timedelta(minutes=30))]

    adapter = SelfMonitorAdapter()

    async def _fake():
        return tweets

    result = await adapter.reconcile(
        db_path=db_path, timeline_fetcher=_fake, now=now
    )
    assert result.bound == 0
    assert result.wild == 1

    conn = get_conn(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM wild_posts").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_reconcile_mixed_batch(cookie_file, db_path) -> None:
    """1 bound + 1 wild + 1 too-old (out of lookback window)."""
    now = _now()
    bind_text = "to be bound"
    bind_did = _insert_draft(db_path, bind_text)
    tweets = [
        _tweet(bind_text, "https://x.com/huajiao_test/status/1", now - timedelta(hours=2)),
        _tweet("orphan", "https://x.com/huajiao_test/status/2", now - timedelta(hours=3)),
        _tweet("ancient", "https://x.com/huajiao_test/status/3", now - timedelta(days=10)),
    ]

    adapter = SelfMonitorAdapter()

    async def _fake():
        return tweets

    result = await adapter.reconcile(
        db_path=db_path, timeline_fetcher=_fake, now=now
    )
    assert result.scanned == 3
    assert result.bound == 1
    assert result.wild == 1
    assert result.errors == 0

    conn = get_conn(db_path)
    try:
        bound_url = conn.execute(
            "SELECT tweet_url FROM drafts WHERE id=?", (bind_did,)
        ).fetchone()["tweet_url"]
        assert bound_url == "https://x.com/huajiao_test/status/1"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_reconcile_swallows_fetcher_error(cookie_file, db_path) -> None:
    adapter = SelfMonitorAdapter()

    async def _boom():
        raise RuntimeError("playwright exploded")

    result = await adapter.reconcile(
        db_path=db_path, timeline_fetcher=_boom, now=_now()
    )
    assert result == result.__class__(scanned=0, bound=0, wild=0, errors=1)


@pytest.mark.asyncio
async def test_reconcile_writes_source_health(cookie_file, db_path) -> None:
    adapter = SelfMonitorAdapter()

    async def _empty():
        return []

    await adapter.reconcile(db_path=db_path, timeline_fetcher=_empty, now=_now())
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT consecutive_failures, last_success_at FROM source_health "
            "WHERE adapter_name=?", ("self_monitor",),
        ).fetchone()
        assert row is not None
        assert row["consecutive_failures"] == 0
        assert row["last_success_at"] is not None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_reconcile_skips_malformed_url(cookie_file, db_path) -> None:
    now = _now()
    tweets = [
        {"text": "no url at all", "url": "", "created_at": now.isoformat()},
        {"text": "garbage url", "url": "not-a-tweet-url",
         "created_at": now.isoformat()},
    ]

    adapter = SelfMonitorAdapter()

    async def _fake():
        return tweets

    result = await adapter.reconcile(
        db_path=db_path, timeline_fetcher=_fake, now=now
    )
    assert result.bound == 0
    assert result.wild == 0
    assert result.errors == 2


# ---------------------------------------------------------------------------
# 3-stage matching (T2.3 — Codex-flagged content-edit fragility)
# ---------------------------------------------------------------------------


def _insert_draft_with_hash(db_path: Path, content: str, *, status: str = "approved") -> int:
    """Insert a draft with content_hash populated, as writer would."""
    from src.content_match import content_hash
    conn = get_conn(db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO drafts (content, content_hash, content_length, "
                "content_mode, optimal_length, topic_lane, persona, "
                "pattern_ids, source_observation_ids, status) "
                "VALUES (?, ?, ?, 'insight', 'short', 'pre_market', "
                "'finance_neutral', '[]', '[]', ?)",
                (content, content_hash(content), len(content), status),
            )
            return int(cur.lastrowid)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_bind_via_exact_hash_when_text_identical(cookie_file, db_path) -> None:
    now = _now()
    text = "盘前快评 中概反弹"
    draft_id = _insert_draft_with_hash(db_path, text)
    tweets = [_tweet(text, "https://x.com/huajiao_test/status/1", now)]

    adapter = SelfMonitorAdapter()

    async def _fake():
        return tweets

    result = await adapter.reconcile(db_path=db_path, timeline_fetcher=_fake, now=now)
    assert result.bound == 1


@pytest.mark.asyncio
async def test_bind_via_normalized_hash_when_punctuation_edited(cookie_file, db_path) -> None:
    """User adds a period in X client — normalized hash still matches."""
    now = _now()
    draft_text = "盘前快评 中概反弹 注意高位锁仓"
    posted_text = "盘前快评：中概反弹，注意高位锁仓。"  # added punctuation
    draft_id = _insert_draft_with_hash(db_path, draft_text)
    tweets = [_tweet(posted_text, "https://x.com/huajiao_test/status/2", now)]

    adapter = SelfMonitorAdapter()

    async def _fake():
        return tweets

    result = await adapter.reconcile(db_path=db_path, timeline_fetcher=_fake, now=now)
    assert result.bound == 1, "normalized-hash match must succeed despite punctuation edit"


@pytest.mark.asyncio
async def test_bind_via_fuzzy_when_single_char_edited(cookie_file, db_path) -> None:
    """User changes ONE Chinese character — fuzzy similarity ≥ 0.85 binds."""
    now = _now()
    draft_text = "盘前快评 中概反弹 注意高位锁仓警惕回调风险"
    posted_text = "盘前快评 中概反弹 注意高位锁仓警惕回调風险"  # 风 → 風
    draft_id = _insert_draft_with_hash(db_path, draft_text)
    tweets = [_tweet(posted_text, "https://x.com/huajiao_test/status/3", now)]

    adapter = SelfMonitorAdapter()

    async def _fake():
        return tweets

    result = await adapter.reconcile(db_path=db_path, timeline_fetcher=_fake, now=now)
    assert result.bound == 1, "fuzzy fallback must catch single-char edits"


@pytest.mark.asyncio
async def test_skips_candidate_status_draft(cookie_file, db_path) -> None:
    """Drafts in 'candidate' status MUST NOT be bound (would skip approval)."""
    now = _now()
    text = "盘前快评 中概反弹"
    _insert_draft_with_hash(db_path, text, status="candidate")
    tweets = [_tweet(text, "https://x.com/huajiao_test/status/4", now)]

    adapter = SelfMonitorAdapter()

    async def _fake():
        return tweets

    result = await adapter.reconcile(db_path=db_path, timeline_fetcher=_fake, now=now)
    assert result.bound == 0
    assert result.wild == 1
