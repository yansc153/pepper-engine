"""CRUD tests for selector.db."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from src.database import get_conn, init_db  # noqa: E402
from selector import db as selector_db  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "x.db"
    init_db(db)
    c = get_conn(db)
    yield c
    c.close()


def _ins(conn, **overrides):
    base = dict(
        source_observation_ids=[1, 2],
        topic_summary="盘前美股大跌",
        virality_score=72.5,
        predicted_content_mode="insight",
        predicted_length="medium",
        predicted_topic_lane="pre_market",
        kol_reaction_count=3,
        emotional_intensity=0.6,
        debate_potential=0.4,
    )
    base.update(overrides)
    return selector_db.insert_candidate(conn, **base)


def test_insert_returns_row_id_and_sets_status_fresh(conn):
    cid = _ins(conn)
    assert cid > 0
    row = conn.execute("SELECT * FROM topic_candidates WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "fresh"
    assert row["virality_score"] == pytest.approx(72.5)
    assert '"1"' not in row["source_observations"]  # stored as JSON list of ints


def test_insert_rejects_bad_mode(conn):
    with pytest.raises(ValueError, match="content_mode"):
        _ins(conn, predicted_content_mode="haha")


def test_insert_rejects_empty_observation_ids(conn):
    with pytest.raises(ValueError):
        _ins(conn, source_observation_ids=[])


def test_insert_rejects_out_of_range_score(conn):
    with pytest.raises(ValueError):
        _ins(conn, virality_score=200)


def test_fetch_fresh_orders_by_score(conn):
    _ins(conn, virality_score=10.0, topic_summary="低")
    high = _ins(conn, virality_score=90.0, topic_summary="高")
    rows = selector_db.fetch_fresh(conn, limit=5)
    assert rows[0]["id"] == high
    assert rows[0]["source_observations"] == [1, 2]  # parsed back to list


def test_fetch_fresh_filters_by_lane(conn):
    _ins(conn, predicted_topic_lane="pre_market", virality_score=10)
    other = _ins(conn, predicted_topic_lane="overnight", virality_score=20)
    rows = selector_db.fetch_fresh(conn, topic_lane="overnight")
    assert len(rows) == 1
    assert rows[0]["id"] == other


def _insert_draft(conn) -> int:
    cur = conn.execute(
        "INSERT INTO drafts (content, content_length, content_mode, optimal_length, "
        "topic_lane, persona, pattern_ids, source_observation_ids) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("dummy", 5, "insight", "short", "pre_market", "ck", "[]", "[]"),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_mark_consumed_flips_status_and_records_draft(conn):
    cid = _ins(conn)
    draft_id = _insert_draft(conn)
    selector_db.mark_consumed(conn, cid, draft_id=draft_id)
    row = conn.execute("SELECT * FROM topic_candidates WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "consumed"
    assert row["consumed_by_draft_id"] == draft_id
    assert row["consumed_at"] is not None


def test_mark_consumed_twice_raises(conn):
    cid = _ins(conn)
    selector_db.mark_consumed(conn, cid, draft_id=None)
    with pytest.raises(ValueError):
        selector_db.mark_consumed(conn, cid, draft_id=None)


def test_expire_old_candidates(conn):
    fresh = _ins(conn)
    stale = _ins(conn, topic_summary="stale")
    # backdate one row
    conn.execute(
        "UPDATE topic_candidates SET generated_at=? WHERE id=?",
        ("2020-01-01 00:00:00", stale),
    )
    conn.commit()
    affected = selector_db.expire_old_candidates(conn, older_than_hours=6)
    assert affected == 1
    statuses = dict(conn.execute("SELECT id,status FROM topic_candidates").fetchall())
    assert statuses[stale] == "expired"
    assert statuses[fresh] == "fresh"
