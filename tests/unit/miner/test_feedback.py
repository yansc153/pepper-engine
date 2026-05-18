"""Unit tests for src.miner.feedback — EMA update + cooling state machine."""

from __future__ import annotations

import json
from pathlib import Path

from src.database import get_conn
from src.miner.feedback import (
    COOLING_MISS_THRESHOLD,
    apply_post_outcome,
)


def _seed_post_and_entries(db: Path, entry_ids: list[int]) -> int:
    """Seed posts + technique_entries + a draft with pattern_ids = entry_ids.

    Returns the DRAFT id (which is what apply_post_outcome now takes; Round-3
    fix: it reads drafts.pattern_ids directly, no longer retrieval_log.post_id).
    """
    conn = get_conn(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO posts (content, content_hash, topic_lane, persona) "
                "VALUES (?,?,?,?)",
                ("hi", "h1", "pre_market", "finance_neutral"),
            )
            post_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            for eid in entry_ids:
                conn.execute(
                    "INSERT INTO reaction_observations (id, source, author_handle, "
                    "author_tier, content, posted_at, likes, retweets, replies, "
                    "has_image, raw_url, viral_score, is_viral) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, "x_list_finance", "a", 1, "x", "2026-05-01 00:00:00",
                     0, 0, 0, 0, f"https://x/{eid}", 0.0, 1),
                )
                conn.execute(
                    "INSERT INTO technique_entries (id, observation_id, hook_pattern, "
                    "hook_example, syntax_signature, sentence_len_avg, sentence_len_p90, "
                    "stance_strength, emotion_triggers, image_style, post_hour_utc, "
                    "topic_lane, applicable_personas, content_mode, optimal_length, "
                    "success_score, recency_weight) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, eid, "数字暴击", "ex", "short_comma_no_period",
                     18.0, 26.0, 3, "[]", "none", 22, "pre_market",
                     '["finance_neutral"]', "insight", "short", 50.0, 1.0),
                )
            # The draft is what apply_post_outcome now keys on.
            conn.execute(
                "INSERT INTO drafts (content, content_length, content_mode, "
                "optimal_length, topic_lane, persona, pattern_ids, "
                "source_observation_ids, status) VALUES "
                "(?, ?, 'insight', 'short', 'pre_market', "
                "'finance_neutral', ?, '[]', 'learned')",
                ("hi", 2, json.dumps(entry_ids)),
            )
            draft_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        return int(draft_id)
    finally:
        conn.close()


def _success(db: Path, eid: int) -> float:
    conn = get_conn(db)
    try:
        row = conn.execute(
            "SELECT success_score FROM technique_entries WHERE id=?", (eid,)
        ).fetchone()
    finally:
        conn.close()
    return float(row["success_score"])


def test_top_outcome_raises_success_score(tmp_db: Path) -> None:
    post_id = _seed_post_and_entries(tmp_db, [1, 2])
    apply_post_outcome(post_id, "top")
    # 50 + 0.3*(100-50) = 65
    assert abs(_success(tmp_db, 1) - 65.0) < 1e-6


def test_bottom_outcome_lowers_score_gently(tmp_db: Path) -> None:
    post_id = _seed_post_and_entries(tmp_db, [1])
    apply_post_outcome(post_id, "bottom")
    # 50 + 0.1*(0-50) = 45
    assert abs(_success(tmp_db, 1) - 45.0) < 1e-6


def test_mid_outcome_does_not_shift_score(tmp_db: Path) -> None:
    post_id = _seed_post_and_entries(tmp_db, [1])
    apply_post_outcome(post_id, "mid")
    assert _success(tmp_db, 1) == 50.0


def test_bottom_outcome_records_cooling(tmp_db: Path) -> None:
    post_id = _seed_post_and_entries(tmp_db, [1])
    for _ in range(COOLING_MISS_THRESHOLD):
        apply_post_outcome(post_id, "bottom")
    conn = get_conn(tmp_db)
    try:
        row = conn.execute(
            "SELECT consecutive_misses, reset_after FROM pattern_cooling "
            "WHERE pattern_id=1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["consecutive_misses"] >= COOLING_MISS_THRESHOLD
    assert row["reset_after"] is not None


def test_top_outcome_resets_cooling(tmp_db: Path) -> None:
    post_id = _seed_post_and_entries(tmp_db, [1])
    apply_post_outcome(post_id, "bottom")
    apply_post_outcome(post_id, "bottom")
    apply_post_outcome(post_id, "top")
    conn = get_conn(tmp_db)
    try:
        row = conn.execute(
            "SELECT consecutive_misses FROM pattern_cooling WHERE pattern_id=1"
        ).fetchone()
    finally:
        conn.close()
    assert row is None, "top outcome must clear cooling"


def test_apply_with_no_retrieval_log_is_noop(tmp_db: Path) -> None:
    # No retrieval log for this post → nothing to update
    conn = get_conn(tmp_db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO posts (content, content_hash, topic_lane, persona) "
                "VALUES ('x','x','pre_market','finance_neutral')"
            )
            post_id = conn.execute(
                "SELECT last_insert_rowid() AS id"
            ).fetchone()["id"]
    finally:
        conn.close()
    apply_post_outcome(int(post_id), "top")  # must not raise
