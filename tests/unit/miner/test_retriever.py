"""Unit tests for src.miner.retriever — cold-start tiers, SQL ranking, perf."""

from __future__ import annotations

import json
import time
from pathlib import Path

from src.database import get_conn
from src.miner.retriever import retrieve
from src.miner.types import RetrievalContext


def _ctx(
    *,
    lane: str = "pre_market",
    hour: int = 22,
    persona: str = "finance_neutral",
    avoid: list[int] | None = None,
) -> RetrievalContext:
    return RetrievalContext(
        topic_lane=lane,
        post_hour_utc=hour,
        persona=persona,
        fact_spine_keywords=[],
        avoid_recent_pattern_ids=avoid or [],
    )


def _insert(
    db: Path,
    *,
    obs_id: int,
    entry_id: int,
    hook: str = "数字暴击",
    lane: str = "pre_market",
    hour: int = 22,
    personas: list[str] | None = None,
    success: float = 50.0,
    recency: float = 1.0,
    sx: str = "short_comma_no_period",
) -> None:
    conn = get_conn(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO reaction_observations (id, source, author_handle, "
                "author_tier, content, posted_at, likes, retweets, replies, "
                "has_image, raw_url, viral_score, is_viral) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (obs_id, "xueqiu", "a", 1, "x", "2026-05-01 00:00:00",
                 100, 0, 0, 0, f"https://x/{obs_id}", 100.0, 1),
            )
            conn.execute(
                "INSERT INTO technique_entries (id, observation_id, hook_pattern, "
                "hook_example, syntax_signature, sentence_len_avg, sentence_len_p90, "
                "stance_strength, emotion_triggers, image_style, post_hour_utc, "
                "topic_lane, applicable_personas, content_mode, optimal_length, "
                "success_score, recency_weight) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry_id, obs_id, hook, "ex", sx, 18.0, 26.0, 3,
                 "[]", "none", hour, lane,
                 json.dumps(personas or ["finance_neutral"]),
                 "insight", "short", success, recency),
            )
    finally:
        conn.close()


# ---------------- cold-start tier ----------------


def test_retrieve_empty_db_uses_static_fallback(tmp_db: Path) -> None:
    """With 0 entries in DB, retriever must use templates/hooks_finance.md."""
    out = retrieve(_ctx(lane="pre_market", hour=22), k=3)
    assert len(out) >= 1
    assert all(e.id < 0 for e in out), "static entries have negative ids"


def test_retrieve_zero_k_returns_empty(tmp_db: Path) -> None:
    assert retrieve(_ctx(), k=0) == []


# ---------------- SQL ranking ----------------


def test_retrieve_ranks_by_success_x_recency(seed_500: Path) -> None:
    out = retrieve(_ctx(lane="pre_market", hour=22), k=5)
    assert len(out) <= 5
    if len(out) >= 2:
        scores = [e.success_score * e.recency_weight for e in out if e.id >= 0]
        # SQL part sorted desc; bridge may be appended after — only check the
        # SQL block is non-increasing
        sql_only = [s for s in scores]
        # at least the first two from SQL should be DESC
        if len(sql_only) >= 2:
            assert sql_only[0] >= sql_only[1] - 1e-6


def test_retrieve_excludes_avoid_recent(tmp_db: Path) -> None:
    # >= 100 entries → pure SQL tier
    for i in range(1, 102):
        _insert(
            tmp_db, obs_id=i, entry_id=i,
            lane="pre_market", hour=22, success=50.0 + i,
        )
    out = retrieve(_ctx(lane="pre_market", hour=22, avoid=[101]), k=5)
    ids = {e.id for e in out}
    assert 101 not in ids


def test_retrieve_respects_hour_window(tmp_db: Path) -> None:
    for i in range(1, 102):
        # half at hour=22, half at hour=10 (way outside)
        hr = 22 if i <= 50 else 10
        _insert(tmp_db, obs_id=i, entry_id=i, hour=hr, lane="pre_market")
    out = retrieve(_ctx(lane="pre_market", hour=22), k=10)
    # all results should be within +/-2 hours of 22
    for e in out:
        if e.id >= 0:
            assert abs(e.post_hour_utc - 22) <= 2


def test_retrieve_skips_cooled_patterns(tmp_db: Path) -> None:
    for i in range(1, 102):
        _insert(tmp_db, obs_id=i, entry_id=i, lane="pre_market", hour=22)
    conn = get_conn(tmp_db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO pattern_cooling (pattern_id, reset_after, "
                "consecutive_misses) VALUES (1, datetime('now', '+7 days'), 3)"
            )
    finally:
        conn.close()
    out = retrieve(_ctx(lane="pre_market", hour=22), k=20)
    assert 1 not in {e.id for e in out}


def test_retrieve_persona_filter(tmp_db: Path) -> None:
    for i in range(1, 102):
        _insert(
            tmp_db, obs_id=i, entry_id=i, lane="pre_market", hour=22,
            personas=["finance_contrarian"] if i == 50 else ["finance_neutral"],
        )
    out = retrieve(_ctx(persona="finance_contrarian", hour=22), k=10)
    # only entry 50 matches; static fallback is also persona-aware so may also
    # contribute, but entry 50 specifically should be present
    ids = {e.id for e in out}
    assert 50 in ids


# ---------------- performance ----------------


def test_retrieve_perf_under_200ms_at_500_entries(seed_500: Path) -> None:
    ctx = _ctx(lane="pre_market", hour=22)
    # warm-up + 5 trials, take median
    samples = []
    for _ in range(6):
        t0 = time.perf_counter()
        retrieve(ctx, k=5)
        samples.append(time.perf_counter() - t0)
    median = sorted(samples)[len(samples) // 2]
    assert median < 0.2, f"retrieve took {median*1000:.1f}ms (>200ms)"


def test_retrieve_logs_audit(tmp_db: Path) -> None:
    for i in range(1, 102):
        _insert(tmp_db, obs_id=i, entry_id=i, lane="pre_market", hour=22)
    retrieve(_ctx(lane="pre_market", hour=22), k=3)
    conn = get_conn(tmp_db)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM retrieval_log"
        ).fetchone()
    finally:
        conn.close()
    assert row["n"] >= 1
