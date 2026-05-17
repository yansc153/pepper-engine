"""Unit tests for src.miner.weaver — recency decay, prune, edge creation."""

from __future__ import annotations

import json
from pathlib import Path

from src.database import get_conn
from src.miner.weaver import (
    RECENCY_DECAY_FACTOR,
    weave_full,
    weave_nightly,
)


def _insert_obs(db: Path, obs_id: int, author: str = "alice") -> None:
    conn = get_conn(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO reaction_observations (id, source, author_handle, "
                "author_tier, content, posted_at, likes, retweets, replies, "
                "has_image, raw_url, viral_score, is_viral) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    obs_id, "xueqiu", author, 1, f"c{obs_id}", "2026-05-01 00:00:00",
                    100, 0, 0, 0, f"https://example.com/{obs_id}", 100.0, 1,
                ),
            )
    finally:
        conn.close()


def _insert_entry(
    db: Path,
    *,
    entry_id: int,
    obs_id: int,
    hook: str = "数字暴击",
    lane: str = "pre_market",
    sx: str = "short_comma_no_period",
    emotions: list[str] | None = None,
    recency: float = 1.0,
    success: float = 50.0,
    used: int = 0,
) -> None:
    conn = get_conn(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO technique_entries (id, observation_id, hook_pattern, "
                "hook_example, syntax_signature, sentence_len_avg, sentence_len_p90, "
                "stance_strength, emotion_triggers, image_style, post_hour_utc, "
                "topic_lane, applicable_personas, content_mode, optimal_length, "
                "success_score, recency_weight, times_used_in_post) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id, obs_id, hook, "ex", sx, 18.0, 26.0, 3,
                    json.dumps(emotions or []), "none", 10, lane,
                    json.dumps(["finance_neutral"]), "insight", "short",
                    success, recency, used,
                ),
            )
    finally:
        conn.close()


def test_weave_nightly_creates_same_hook_edge(tmp_db: Path) -> None:
    _insert_obs(tmp_db, 1)
    _insert_obs(tmp_db, 2)
    _insert_entry(tmp_db, entry_id=1, obs_id=1, hook="数字暴击", lane="pre_market")
    _insert_entry(tmp_db, entry_id=2, obs_id=2, hook="数字暴击", lane="intraday")
    created = weave_nightly([2])
    assert created >= 1
    conn = get_conn(tmp_db)
    try:
        rows = conn.execute(
            "SELECT edge_type, weight FROM technique_edges"
        ).fetchall()
    finally:
        conn.close()
    types = {r["edge_type"] for r in rows}
    assert "same_hook" in types


def test_weave_nightly_respects_recency_floor(tmp_db: Path) -> None:
    _insert_obs(tmp_db, 1)
    _insert_obs(tmp_db, 2)
    # entry 1 below floor → invisible candidate
    _insert_entry(tmp_db, entry_id=1, obs_id=1, recency=0.1)
    _insert_entry(tmp_db, entry_id=2, obs_id=2, recency=1.0)
    created = weave_nightly([2])
    assert created == 0  # 1 was floored out


def test_weave_nightly_ignores_negative_ids(tmp_db: Path) -> None:
    assert weave_nightly([-1, -5]) == 0


def test_weave_nightly_no_duplicates(tmp_db: Path) -> None:
    _insert_obs(tmp_db, 1)
    _insert_obs(tmp_db, 2)
    _insert_entry(tmp_db, entry_id=1, obs_id=1, hook="数字暴击")
    _insert_entry(tmp_db, entry_id=2, obs_id=2, hook="数字暴击")
    weave_nightly([2])
    second = weave_nightly([2])
    # all edges already exist, UPSERT IGNORE → 0 new
    assert second == 0


def test_weave_full_decays_recency(tmp_db: Path) -> None:
    _insert_obs(tmp_db, 1)
    # used>0 so prune skips it and we can inspect the decayed weight.
    _insert_entry(tmp_db, entry_id=1, obs_id=1, recency=1.0, used=1)
    decayed, _ = weave_full()
    assert decayed == 1
    conn = get_conn(tmp_db)
    try:
        row = conn.execute(
            "SELECT recency_weight FROM technique_entries WHERE id=1"
        ).fetchone()
    finally:
        conn.close()
    assert abs(row["recency_weight"] - RECENCY_DECAY_FACTOR) < 1e-9


def test_weave_full_prunes_bottom_quintile(tmp_db: Path) -> None:
    for i in range(1, 11):
        _insert_obs(tmp_db, i)
        _insert_entry(
            tmp_db, entry_id=i, obs_id=i, success=float(i * 10), used=0
        )
    _, pruned = weave_full()
    assert pruned >= 1
    conn = get_conn(tmp_db)
    try:
        rows = conn.execute(
            "SELECT MIN(success_score) AS lo FROM technique_entries"
        ).fetchone()
    finally:
        conn.close()
    assert rows["lo"] > 10  # bottom entries gone


def test_weave_full_keeps_used_entries(tmp_db: Path) -> None:
    _insert_obs(tmp_db, 1)
    _insert_entry(tmp_db, entry_id=1, obs_id=1, success=1.0, used=5)
    _insert_obs(tmp_db, 2)
    _insert_entry(tmp_db, entry_id=2, obs_id=2, success=99.0, used=0)
    weave_full()
    conn = get_conn(tmp_db)
    try:
        rows = conn.execute(
            "SELECT id FROM technique_entries ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    ids = {r["id"] for r in rows}
    assert 1 in ids, "used entries must not be pruned"


def test_weave_full_empty_db_is_noop(tmp_db: Path) -> None:
    decayed, pruned = weave_full()
    assert decayed == 0
    assert pruned == 0
