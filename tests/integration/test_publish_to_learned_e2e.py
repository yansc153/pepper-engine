"""End-to-end regression: approved draft → bind → metrics → 'learned'.

Codex Round-2 reproduced this bug:
    {'status': 'learned', 'timeseries_rows': 0, 'posts_rows': 0}
i.e. state advanced to 'learned' but no posts row existed and no metrics
were ever written. Caused by reviewer using ``metrics_by_draft.keys()``
for status advancement instead of the set actually persisted to timeseries.

This test pins the fixed behavior:
  1. seed a draft in 'approved' status (manual mode just past Discord ✅)
  2. run self_monitor binding — verify it BOTH updates drafts AND creates posts
  3. run reviewer — verify timeseries row exists
  4. verify status advanced to 'learned' (and ONLY because timeseries was written)

Also pins the negative case: a draft bound WITHOUT a posts row stays at
'published' (not 'learned'), to prove the defensive guard works.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.database import get_conn


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "pepperbot.db"
    from src.migrations.runner import run_migrations
    run_migrations(db_path=db, verbose=False)
    # database.DB_PATH is read once at import — must monkeypatch the module
    # attribute so reviewer/self_monitor's get_conn() lands on this temp DB.
    import src.database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db)
    return db


def _insert_technique_entry(db: Path) -> int:
    """Seed a technique_entries row so apply_post_outcome has something to bump."""
    conn = get_conn(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO reaction_observations "
                "(source, author_handle, author_tier, content, posted_at, "
                "likes, retweets, replies, has_image, raw_url, viral_score, is_viral) "
                "VALUES ('xueqiu','seed',1,'x','2026-05-01 00:00:00',0,0,0,0,"
                "'https://x/seed',0.0,1)",
            )
            obs_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            cur = conn.execute(
                "INSERT INTO technique_entries "
                "(observation_id, hook_pattern, hook_example, syntax_signature, "
                "sentence_len_avg, sentence_len_p90, stance_strength, "
                "emotion_triggers, image_style, post_hour_utc, topic_lane, "
                "applicable_personas, content_mode, optimal_length, "
                "success_score, times_used_in_post, recency_weight) VALUES "
                "(?, '数字反差', '去年同期X，今年Y', 'short_comma_no_period', "
                "18.0, 26.0, 3, '[]', 'none', 22, 'intraday', "
                "'[\"finance_neutral\"]', 'insight', 'short', 50.0, 0, 1.0)",
                (obs_id,),
            )
            return int(cur.lastrowid)
    finally:
        conn.close()


def _insert_approved_draft(
    db: Path, *, content: str, draft_id: int = 1,
    pattern_ids: list[int] | None = None,
) -> None:
    import json
    from src.content_match import content_hash
    pat_json = json.dumps(pattern_ids or [])
    conn = get_conn(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO drafts "
                "(id, content, content_hash, content_length, content_mode, "
                "optimal_length, topic_lane, persona, pattern_ids, "
                "source_observation_ids, image_path, status) "
                "VALUES (?, ?, ?, ?, 'insight', 'short', 'intraday', "
                "'finance_neutral', ?, '[]', NULL, 'approved')",
                (draft_id, content, content_hash(content), len(content), pat_json),
            )
    finally:
        conn.close()


def test_e2e_approved_to_learned(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The full happy path Codex demanded: bind creates posts → reviewer writes
    timeseries → status advances to 'learned' → pattern usage counter bumps.

    Uses draft_id=7 and pre-seeds throwaway posts so posts.id != draft_id —
    pins that learning works even when those ids diverge (Codex Round-3
    concern: prior happy-path test had draft.id==posts.id==1 by coincidence).
    """
    monkeypatch.setenv("TWITTER_HANDLE", "off_tehtarget")
    technique_id = _insert_technique_entry(tmp_db)
    # Pre-fill 2 throwaway posts so posts.id starts at 3 (≠ draft_id=7)
    conn = get_conn(tmp_db)
    try:
        with conn:
            for i in range(2):
                conn.execute(
                    "INSERT INTO posts (content, content_hash, topic_lane, "
                    "persona, status) VALUES (?, ?, 'intraday', 'finance_neutral', 'pending')",
                    (f"filler{i}", f"hash_{i}"),
                )
    finally:
        conn.close()

    _insert_approved_draft(
        tmp_db,
        draft_id=7,
        content="盘前快评 中概反弹注意高位锁仓",
        pattern_ids=[technique_id],
    )

    # Manually advance approved → pushed_to_discord so binding window matches
    # (binding looks for ('pushed_to_discord','approved') — both work, but in
    # manual mode the bound state was already approved).
    # No advance needed; 'approved' is accepted by _bind_draft.

    # --- Step 1: simulate self_monitor binding ---
    from src.observers.self_monitor_adapter import _bind_draft
    now = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
    bound = _bind_draft(
        text="盘前快评 中概反弹注意高位锁仓",
        url="https://x.com/off_tehtarget/status/9999",
        posted_at=now,
        db_path=tmp_db,
    )
    assert bound is True

    # Verify BOTH drafts AND posts rows now exist for this tweet_url, AND
    # that posts.id (3) != draft_id (7) — diverging ids prove we don't rely
    # on accidental id-equality (Codex Round-3 concern).
    conn = get_conn(tmp_db)
    try:
        d = conn.execute(
            "SELECT status, tweet_url FROM drafts WHERE id=7"
        ).fetchone()
        p = conn.execute(
            "SELECT id, tweet_url, status FROM posts WHERE tweet_url=?",
            ("https://x.com/off_tehtarget/status/9999",),
        ).fetchone()
    finally:
        conn.close()
    assert d["status"] == "published"
    assert d["tweet_url"] == "https://x.com/off_tehtarget/status/9999"
    assert p is not None, "binding MUST create a posts row (Round-2 regression)"
    assert p["status"] == "published"
    assert p["id"] != 7, (
        "this test relies on posts.id != draft_id to catch any code path "
        "that mistakenly treats them as interchangeable"
    )

    # --- Step 2: run reviewer with a fake metrics fetcher ---
    from src import reviewer as rv

    async def fake_fetcher(url: str) -> dict[str, int]:
        assert url == "https://x.com/off_tehtarget/status/9999"
        return {"likes": 80, "retweets": 5, "replies": 12, "impressions": 1500}

    report = asyncio.run(rv.review_and_update_weights(
        window_days=7, metrics_fetcher=fake_fetcher
    ))

    # --- Step 3: verify timeseries + status advanced + LEARNING happened ---
    conn = get_conn(tmp_db)
    try:
        ts = conn.execute(
            "SELECT viral_score FROM post_metrics_timeseries "
            "WHERE post_id=?", (p["id"],),
        ).fetchall()
        final = conn.execute(
            "SELECT status FROM drafts WHERE id=7"
        ).fetchone()
        tech = conn.execute(
            "SELECT success_score, times_used_in_post "
            "FROM technique_entries WHERE id=?",
            (technique_id,),
        ).fetchone()
    finally:
        conn.close()

    assert len(ts) == 1, "reviewer MUST write timeseries row"
    assert report.metrics_collected == 1
    assert final["status"] == "learned"
    # Round-3 fix proof: pattern_ids → apply_post_outcome → technique_entries
    # actually update. Pre-fix this assertion failed:
    #   apply_post_outcome(draft_id) used retrieval_log.post_id which was
    #   never populated, so entry_ids was always [] and nothing changed.
    assert tech["times_used_in_post"] >= 1 or tech["success_score"] != 50.0, (
        "learning loop MUST update technique_entries — proves Codex Round-3 fix"
    )


def test_e2e_draft_without_posts_row_stays_published(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive guard: a draft that somehow has tweet_url but no posts row
    (legacy data, manual SQL, broken binding) MUST NOT advance to 'learned'.

    This is the exact silent-failure mode Codex reproduced before the fix.
    """
    monkeypatch.setenv("TWITTER_HANDLE", "off_tehtarget")
    # Insert a draft with tweet_url set DIRECTLY (no posts row — simulates the
    # broken pre-fix world where binding only touched drafts).
    conn = get_conn(tmp_db)
    try:
        with conn:
            from src.content_match import content_hash
            conn.execute(
                "INSERT INTO drafts "
                "(id, content, content_hash, content_length, content_mode, "
                "optimal_length, topic_lane, persona, pattern_ids, "
                "source_observation_ids, status, tweet_url, posted_at) "
                "VALUES (1, 'orphan tweet', ?, 12, 'insight', 'short', "
                "'intraday', 'finance_neutral', '[]', '[]', 'published', "
                "'https://x.com/u/status/1', CURRENT_TIMESTAMP)",
                (content_hash("orphan tweet"),),
            )
    finally:
        conn.close()

    from src import reviewer as rv

    async def fake_fetcher(url: str) -> dict[str, int]:
        return {"likes": 100, "retweets": 10, "replies": 5, "impressions": 2000}

    report = asyncio.run(rv.review_and_update_weights(
        window_days=7, metrics_fetcher=fake_fetcher
    ))

    conn = get_conn(tmp_db)
    try:
        ts_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM post_metrics_timeseries"
        ).fetchone()
        final = conn.execute(
            "SELECT status FROM drafts WHERE id=1"
        ).fetchone()
    finally:
        conn.close()

    assert ts_rows["n"] == 0, "no posts row → no timeseries write"
    assert report.metrics_collected == 0
    assert final["status"] == "published", (
        "draft without posts row MUST NOT advance to learned — fixes the exact "
        "silent state drift Codex Round-2 reproduced"
    )
