"""Unit tests for src.database and migrations runner."""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# allow `from src...` imports regardless of pytest CWD
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database import (  # noqa: E402
    get_conn,
    init_db,
    list_known_migrations,
    load_schema_migrations,
    with_retry,
)
from src.migrations.runner import run_migrations  # noqa: E402

EXPECTED_TABLES = {
    "posts",
    "reaction_observations",
    "strategy_weights",
    "learning_log",
    "source_health",
    "circuit_breaker",
    "slop_words",
    "daily_stats",
    "technique_entries",
    "technique_edges",
    "retrieval_log",
    "post_metrics_timeseries",
    "drafts",
    "topic_candidates",
    "wild_posts",
    "human_rejection_pool",
    "pattern_cooling",
    "schema_migrations",
}


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    init_db(p)
    return p


def _table_names(p: Path) -> set[str]:
    conn = get_conn(p)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r["name"] for r in rows}
    finally:
        conn.close()


def test_init_db_creates_all_tables(db: Path) -> None:
    tables = _table_names(db)
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables: {missing}"


def test_init_db_idempotent(db: Path) -> None:
    # Running again must apply zero migrations and not raise.
    new = init_db(db)
    assert new == []
    assert _table_names(db) >= EXPECTED_TABLES


def test_schema_migrations_records_all(db: Path) -> None:
    applied = load_schema_migrations(db)
    known = list_known_migrations()
    assert applied == known, f"order mismatch: {applied} vs {known}"


def test_migrations_run_in_filename_order(tmp_path: Path) -> None:
    p = tmp_path / "ord.db"
    applied = run_migrations(p, verbose=False)
    assert applied == sorted(applied)
    assert applied[0].startswith("001_")


def test_with_retry_recovers_from_busy() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert with_retry(flaky, retries=5, backoff=0.001) == "ok"
    assert calls["n"] == 3


def test_with_retry_gives_up_after_exhaustion() -> None:
    def always_busy() -> None:
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        with_retry(always_busy, retries=2, backoff=0.001)


def test_insert_select_each_table(db: Path) -> None:
    conn = get_conn(db)
    now = datetime.utcnow().isoformat()
    with conn:
        conn.execute(
            "INSERT INTO reaction_observations (source, author_handle, author_tier,"
            " content, posted_at, likes, retweets, replies, has_image, raw_url,"
            " viral_score, is_viral) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("x", "@a", 1, "hi", now, 10, 5, 2, 1, "https://x.com/1", 12.5, 1),
        )
        obs_id = conn.execute("SELECT id FROM reaction_observations").fetchone()["id"]
        conn.execute(
            "INSERT INTO posts (content, content_hash, topic_lane, persona)"
            " VALUES (?,?,?,?)",
            ("body", "h1", "macro", "finance_v1"),
        )
        post_id = conn.execute("SELECT id FROM posts").fetchone()["id"]
        conn.execute(
            "INSERT INTO strategy_weights (topic_lane, weight) VALUES (?,?)",
            ("macro", 1.0),
        )
        conn.execute(
            "INSERT INTO learning_log (window_days, sample_size) VALUES (?,?)",
            (7, 100),
        )
        conn.execute(
            "INSERT INTO source_health (adapter_name) VALUES (?)", ("x_list_finance",)
        )
        conn.execute(
            "INSERT INTO circuit_breaker (scope, reason) VALUES (?,?)",
            ("publisher", "rate"),
        )
        conn.execute(
            "INSERT INTO slop_words (word, category) VALUES (?,?)",
            ("赋能", "a"),
        )
        conn.execute(
            "INSERT INTO daily_stats (date, posts_published, tokens_spent)"
            " VALUES (?,?,?)",
            ("2026-05-17", 3, 1234),
        )
        conn.execute(
            "INSERT INTO technique_entries (observation_id, hook_pattern,"
            " hook_example, syntax_signature, sentence_len_avg, sentence_len_p90,"
            " stance_strength, emotion_triggers, image_style, post_hour_utc,"
            " topic_lane, applicable_personas, content_mode, optimal_length,"
            " success_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                obs_id, "反问开场", "真的吗？", "Q+stmt", 12.3, 18.0, 5,
                json.dumps(["怒"]), "candid", 7, "macro",
                json.dumps(["finance_v1"]), "insight", "short", 0.82,
            ),
        )
        te_id = conn.execute("SELECT id FROM technique_entries").fetchone()["id"]
        # Need a 2nd entry for an edge
        conn.execute(
            "INSERT INTO reaction_observations (source, author_handle, author_tier,"
            " content, posted_at, likes, retweets, replies, has_image, raw_url,"
            " viral_score, is_viral) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("x", "@b", 1, "hi2", now, 1, 0, 0, 0, "https://x.com/2", 1.0, 0),
        )
        obs_id2 = conn.execute(
            "SELECT id FROM reaction_observations ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO technique_entries (observation_id, hook_pattern,"
            " hook_example, syntax_signature, sentence_len_avg, sentence_len_p90,"
            " stance_strength, emotion_triggers, image_style, post_hour_utc,"
            " topic_lane, applicable_personas, content_mode, optimal_length,"
            " success_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (obs_id2, "p2", "e2", "s2", 10.0, 12.0, 4, "[]", "candid", 8,
             "macro", "[]", "insight", "short", 0.6),
        )
        te_id2 = conn.execute(
            "SELECT id FROM technique_entries ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        a, b = sorted((te_id, te_id2))
        conn.execute(
            "INSERT INTO technique_edges (src_entry_id, dst_entry_id, edge_type,"
            " weight) VALUES (?,?,?,?)",
            (a, b, "co_occur", 0.5),
        )
        conn.execute(
            "INSERT INTO retrieval_log (post_id, retrieved_entry_ids,"
            " context_signature) VALUES (?,?,?)",
            (post_id, json.dumps([te_id]), "sig"),
        )
        conn.execute(
            "INSERT INTO post_metrics_timeseries (post_id, collected_at, likes,"
            " retweets, replies, viral_score) VALUES (?,?,?,?,?,?)",
            (post_id, now, 1, 0, 0, 0.5),
        )
        conn.execute(
            "INSERT INTO drafts (content, content_length, content_mode,"
            " optimal_length, topic_lane, persona, pattern_ids,"
            " source_observation_ids) VALUES (?,?,?,?,?,?,?,?)",
            (
                "draft body", 10, "insight", "short", "macro", "finance_v1",
                json.dumps([te_id]), json.dumps([obs_id]),
            ),
        )
        draft_id = conn.execute("SELECT id FROM drafts").fetchone()["id"]
        conn.execute(
            "INSERT INTO topic_candidates (source_observations, topic_summary,"
            " virality_score) VALUES (?,?,?)",
            (json.dumps([obs_id]), "summary", 88.0),
        )
        conn.execute(
            "INSERT INTO wild_posts (tweet_url, content, content_hash, posted_at)"
            " VALUES (?,?,?,?)",
            ("https://x.com/wild/1", "wild", "wh", now),
        )
        conn.execute(
            "INSERT INTO human_rejection_pool (draft_id, scorer_score, pattern_ids)"
            " VALUES (?,?,?)",
            (draft_id, 75, json.dumps([te_id])),
        )
        conn.execute(
            "INSERT INTO pattern_cooling (pattern_id, reset_after,"
            " consecutive_misses) VALUES (?,?,?)",
            (te_id, (datetime.utcnow() + timedelta(days=7)).isoformat(), 3),
        )
    # Verify counts
    for table in EXPECTED_TABLES - {"schema_migrations"}:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
        assert row["c"] >= 1, f"{table} insert failed"
    conn.close()


def test_unique_constraint_posts_content_hash(db: Path) -> None:
    conn = get_conn(db)
    with conn:
        conn.execute(
            "INSERT INTO posts (content, content_hash, topic_lane, persona)"
            " VALUES (?,?,?,?)",
            ("a", "dup", "macro", "p"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO posts (content, content_hash, topic_lane, persona)"
                " VALUES (?,?,?,?)",
                ("b", "dup", "macro", "p"),
            )
    conn.close()


def test_check_constraint_technique_edges_src_lt_dst(db: Path) -> None:
    conn = get_conn(db)
    # seed 2 entries
    now = datetime.utcnow().isoformat()
    with conn:
        conn.execute(
            "INSERT INTO reaction_observations (source, author_handle, author_tier,"
            " content, posted_at, likes, retweets, replies, has_image, raw_url,"
            " viral_score, is_viral) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("x", "@a", 1, "c", now, 1, 0, 0, 0, "https://x.com/e1", 1.0, 0),
        )
        o1 = conn.execute("SELECT MAX(id) AS id FROM reaction_observations").fetchone()["id"]
        conn.execute(
            "INSERT INTO reaction_observations (source, author_handle, author_tier,"
            " content, posted_at, likes, retweets, replies, has_image, raw_url,"
            " viral_score, is_viral) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("x", "@b", 1, "c", now, 1, 0, 0, 0, "https://x.com/e2", 1.0, 0),
        )
        o2 = conn.execute("SELECT MAX(id) AS id FROM reaction_observations").fetchone()["id"]
        for oid in (o1, o2):
            conn.execute(
                "INSERT INTO technique_entries (observation_id, hook_pattern,"
                " hook_example, syntax_signature, sentence_len_avg, sentence_len_p90,"
                " stance_strength, emotion_triggers, image_style, post_hour_utc,"
                " topic_lane, applicable_personas, content_mode, optimal_length,"
                " success_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, "p", "e", "s", 1.0, 1.0, 1, "[]", "x", 0, "x", "[]",
                 "insight", "short", 0.5),
            )
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM technique_entries ORDER BY id"
        ).fetchall()]
    a, b = ids[-2], ids[-1]
    # src >= dst must fail
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO technique_edges (src_entry_id, dst_entry_id,"
                " edge_type, weight) VALUES (?,?,?,?)",
                (b, a, "co_occur", 0.5),
            )
    # legal direction must pass
    with conn:
        conn.execute(
            "INSERT INTO technique_edges (src_entry_id, dst_entry_id,"
            " edge_type, weight) VALUES (?,?,?,?)",
            (a, b, "co_occur", 0.5),
        )
    # UNIQUE(src, dst, edge_type) must fail
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO technique_edges (src_entry_id, dst_entry_id,"
                " edge_type, weight) VALUES (?,?,?,?)",
                (a, b, "co_occur", 0.9),
            )
    conn.close()


def test_foreign_key_enforced(db: Path) -> None:
    conn = get_conn(db)
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO technique_entries (observation_id, hook_pattern,"
                " hook_example, syntax_signature, sentence_len_avg, sentence_len_p90,"
                " stance_strength, emotion_triggers, image_style, post_hour_utc,"
                " topic_lane, applicable_personas, content_mode, optimal_length,"
                " success_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (999999, "p", "e", "s", 1.0, 1.0, 1, "[]", "x", 0, "x", "[]",
                 "insight", "short", 0.5),
            )
    conn.close()


def test_drafts_status_check_constraint(db: Path) -> None:
    conn = get_conn(db)
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO drafts (content, content_length, content_mode,"
                " optimal_length, topic_lane, persona, pattern_ids,"
                " source_observation_ids, status) VALUES"
                " (?,?,?,?,?,?,?,?,?)",
                ("x", 1, "insight", "short", "m", "p", "[]", "[]", "bogus"),
            )
    conn.close()
