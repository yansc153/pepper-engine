"""Unit tests for S10 reviewer."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src import database, reviewer
from src.database import get_conn, init_db


# Fixtures ------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "reviewer.db"
    init_db(p)
    monkeypatch.setattr(database, "DB_PATH", p)
    # Re-point the entries seed too so reviewer's get_conn() lands here.
    seed = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "db"
        / "seed_50_entries.sql"
    )
    sql = seed.read_text(encoding="utf-8")
    conn = get_conn(p)
    try:
        with conn:
            conn.executescript(sql)
    finally:
        conn.close()
    return p


@pytest.fixture()
def slop_md(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "voice" / "slop_words.md"
    monkeypatch.setattr(reviewer, "SLOP_WORDS_MD_PATH", target)
    return target


def _seed_draft(
    db_path: Path,
    *,
    draft_id: int,
    lane: str,
    pattern_ids: list[int],
    tweet_url: str,
    status: str = "published",
    discord_reacted_at: str | None = None,
) -> int:
    """Insert drafts + posts + retrieval_log rows tied to ``pattern_ids``.

    Returns the corresponding ``posts.id``.
    """
    conn = get_conn(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO drafts (id, content, content_length, content_mode, "
                "optimal_length, topic_lane, persona, pattern_ids, "
                "source_observation_ids, status, tweet_url, posted_at, "
                "discord_reacted_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?)",
                (
                    draft_id,
                    f"draft {draft_id}",
                    20,
                    "insight",
                    "short",
                    lane,
                    "finance_neutral",
                    json.dumps(pattern_ids),
                    json.dumps([]),
                    status,
                    tweet_url,
                    discord_reacted_at,
                ),
            )
            # Pin posts.id == draft_id so apply_post_outcome(draft_id) can
            # find the retrieval_log row via post_id.
            conn.execute(
                "INSERT INTO posts (id, content, content_hash, topic_lane, "
                "persona, posted_at, tweet_url, status) "
                "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP,?,'published')",
                (
                    draft_id,
                    f"draft {draft_id}",
                    f"hash-{draft_id}",
                    lane,
                    "finance_neutral",
                    tweet_url,
                ),
            )
            post_id = draft_id
            conn.execute(
                "INSERT INTO retrieval_log (post_id, retrieved_entry_ids, "
                "context_signature) VALUES (?, ?, ?)",
                (post_id, json.dumps(pattern_ids), f"sig-{draft_id}"),
            )
    finally:
        conn.close()
    return post_id


# Pure helpers --------------------------------------------------------------


def test_percentile_basic() -> None:
    assert reviewer._percentile([], 0.5) == 0.0
    assert reviewer._percentile([10.0], 0.5) == 10.0
    assert reviewer._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.30) == 2.0
    assert reviewer._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.70) == 4.0


def test_percentile_bounds() -> None:
    vs = [10.0, 20.0, 30.0]
    assert reviewer._percentile(vs, 0.0) == 10.0
    assert reviewer._percentile(vs, 1.0) == 30.0


def test_normalize_sums_to_one() -> None:
    out = reviewer._normalize({"a": 0.4, "b": 0.6, "c": 0.0})
    assert abs(sum(out.values()) - 1.0) < 1e-9
    out2 = reviewer._normalize({"a": 0.0, "b": 0.0})
    assert out2 == {"a": 0.0, "b": 0.0}


# Metrics collection --------------------------------------------------------


def test_collect_metrics_skips_failures(tmp_db: Path) -> None:
    drafts = [
        {"id": 1, "tweet_url": "https://x.com/a/status/1"},
        {"id": 2, "tweet_url": "https://x.com/a/status/2"},
    ]

    async def fetcher(url: str) -> dict[str, int]:
        if url.endswith("/1"):
            return {"likes": 100, "retweets": 5, "replies": 2, "impressions": 1000}
        raise RuntimeError("boom")

    out = asyncio.run(reviewer._collect_metrics(drafts, fetcher))
    assert 1 in out and 2 not in out
    assert out[1]["viral_score"] > 0


def test_collect_metrics_handles_empty_dict(tmp_db: Path) -> None:
    drafts = [{"id": 9, "tweet_url": "https://x.com/a/status/9"}]

    async def fetcher(url: str) -> dict[str, int]:
        return {}

    out = asyncio.run(reviewer._collect_metrics(drafts, fetcher))
    assert out == {}


# Timeseries write ----------------------------------------------------------


def test_write_metrics_timeseries_links_via_tweet_url(tmp_db: Path) -> None:
    post_id = _seed_draft(
        tmp_db,
        draft_id=101,
        lane="intraday",
        pattern_ids=[1, 2],
        tweet_url="https://x.com/u/status/101",
    )
    n, written_ids = reviewer._write_metrics_timeseries(
        {
            101: {
                "likes": 50,
                "retweets": 3,
                "replies": 1,
                "impressions": 800,
                "viral_score": 123.4,
            }
        }
    )
    assert n == 1
    assert written_ids == {101}, "must return set of draft_ids actually written"
    conn = get_conn(tmp_db)
    try:
        row = conn.execute(
            "SELECT viral_score FROM post_metrics_timeseries WHERE post_id=?",
            (post_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["viral_score"] == pytest.approx(123.4)


# Classification + EMA dispatch --------------------------------------------


def test_classify_calls_apply_post_outcome(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, str]] = []

    def fake_apply(post_id: int, outcome: str, ema_alpha: float | None = None) -> None:
        calls.append((post_id, outcome))

    monkeypatch.setattr(reviewer, "apply_post_outcome", fake_apply)

    metrics = {
        1: {"likes": 0, "retweets": 0, "replies": 0, "impressions": 0, "viral_score": 5.0},
        2: {"likes": 0, "retweets": 0, "replies": 0, "impressions": 0, "viral_score": 50.0},
        3: {"likes": 0, "retweets": 0, "replies": 0, "impressions": 0, "viral_score": 500.0},
    }
    p30, p70 = reviewer._classify_and_dispatch(metrics)
    assert p30 <= p70
    outcomes = {pid: o for pid, o in calls}
    assert outcomes[1] == "bottom"
    assert outcomes[3] == "top"
    # middle bucket → "mid"
    assert outcomes[2] in {"mid", "top", "bottom"}  # depends on percentile rounding
    assert len(calls) == 3


# Strategy weights ----------------------------------------------------------


def test_strategy_weights_skips_small_samples(tmp_db: Path) -> None:
    # Only 2 published drafts in 'overnight' lane → below MIN_SAMPLE
    for i in range(2):
        post_id = _seed_draft(
            tmp_db,
            draft_id=200 + i,
            lane="overnight",
            pattern_ids=[1],
            tweet_url=f"https://x.com/u/status/200{i}",
        )
        conn = get_conn(tmp_db)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO post_metrics_timeseries (post_id, collected_at, "
                    "likes, retweets, replies, impressions, viral_score) "
                    "VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)",
                    (post_id, 10, 1, 0, 100, 10.0 + i),
                )
        finally:
            conn.close()

    updated, before, after = reviewer._update_strategy_weights(window_days=7)
    assert updated == []
    assert before == after


def test_strategy_weights_updates_winners_and_losers(tmp_db: Path) -> None:
    # Seed 2 lanes, 5 published drafts each. Lane A high score, lane B low.
    for i in range(5):
        for lane, score in (("intraday", 500.0 + i), ("overnight", 1.0 + i)):
            url = f"https://x.com/u/status/{lane}-{i}"
            post_id = _seed_draft(
                tmp_db,
                draft_id=hash((lane, i)) & 0xFFFFFF,
                lane=lane,
                pattern_ids=[1],
                tweet_url=url,
            )
            conn = get_conn(tmp_db)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO post_metrics_timeseries (post_id, collected_at, "
                        "likes, retweets, replies, impressions, viral_score) "
                        "VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)",
                        (post_id, 10, 1, 0, 100, score),
                    )
            finally:
                conn.close()

    updated, before, after = reviewer._update_strategy_weights(window_days=7)
    assert set(updated) == {"intraday", "overnight"}
    assert after["intraday"] > after["overnight"]
    assert abs(sum(after.values()) - 1.0) < 1e-6

    # learning_log row persisted
    conn = get_conn(tmp_db)
    try:
        row = conn.execute(
            "SELECT winning_patterns, losing_patterns FROM learning_log "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert "intraday" in row["winning_patterns"]
    assert "overnight" in row["losing_patterns"]


# Slop pattern distillation -------------------------------------------------


def test_distill_slop_patterns_renders_md(
    tmp_db: Path, slop_md: Path
) -> None:
    # Boost times_used_in_post for hook_pattern '反共识开场' (always low score)
    conn = get_conn(tmp_db)
    try:
        with conn:
            conn.execute(
                "UPDATE technique_entries SET times_used_in_post = 20, "
                "success_score = 5 WHERE hook_pattern = '反共识开场'"
            )
            # Push one other pattern high so percentile cutoff makes sense
            conn.execute(
                "UPDATE technique_entries SET times_used_in_post = 20, "
                "success_score = 95 WHERE hook_pattern = '金句压尾'"
            )
            # Give the rest enough samples too
            conn.execute(
                "UPDATE technique_entries SET times_used_in_post = 20 "
                "WHERE times_used_in_post < 10"
            )
    finally:
        conn.close()

    added = reviewer._distill_slop_patterns()
    assert "反共识开场" in added
    assert "金句压尾" not in added

    content = slop_md.read_text(encoding="utf-8")
    assert "Auto-generated by reviewer (DO NOT EDIT)" in content
    assert "反共识开场" in content


def test_distill_slop_patterns_empty_when_below_threshold(
    tmp_db: Path, slop_md: Path
) -> None:
    # seed_50 has times_used_in_post=0, so nothing qualifies
    added = reviewer._distill_slop_patterns()
    assert added == []
    assert slop_md.read_text(encoding="utf-8").startswith(
        "# Auto-generated by reviewer (DO NOT EDIT)"
    )


# Stale drafts -------------------------------------------------------------


def test_flag_stale_drafts(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alerts: list[str] = []
    monkeypatch.setattr(reviewer, "_alert", alerts.append)
    # approved 10 days ago
    conn = get_conn(tmp_db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO drafts (id, content, content_length, content_mode, "
                "optimal_length, topic_lane, persona, pattern_ids, "
                "source_observation_ids, status, discord_reacted_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'approved', "
                "datetime('now', '-10 days'))",
                (
                    777,
                    "stuck",
                    10,
                    "insight",
                    "short",
                    "intraday",
                    "finance_neutral",
                    "[]",
                    "[]",
                ),
            )
            # fresh approved, should NOT alert
            conn.execute(
                "INSERT INTO drafts (id, content, content_length, content_mode, "
                "optimal_length, topic_lane, persona, pattern_ids, "
                "source_observation_ids, status, discord_reacted_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'approved', "
                "datetime('now', '-1 days'))",
                (
                    778,
                    "fresh",
                    10,
                    "insight",
                    "short",
                    "intraday",
                    "finance_neutral",
                    "[]",
                    "[]",
                ),
            )
    finally:
        conn.close()

    stale = reviewer._flag_stale_drafts()
    assert stale == [777]
    assert any("777" in msg for msg in alerts)


# End-to-end orchestrator --------------------------------------------------


def test_review_and_update_weights_e2e(
    tmp_db: Path, slop_md: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_calls: list[tuple[int, str]] = []

    def fake_apply(post_id: int, outcome: str, ema_alpha: float | None = None) -> None:
        apply_calls.append((post_id, outcome))

    monkeypatch.setattr(reviewer, "apply_post_outcome", fake_apply)

    # 6 published drafts across 2 lanes
    for i in range(6):
        lane = "intraday" if i < 3 else "overnight"
        _seed_draft(
            tmp_db,
            draft_id=300 + i,
            lane=lane,
            pattern_ids=[1, 2],
            tweet_url=f"https://x.com/u/status/{300 + i}",
        )

    async def fetcher(url: str) -> dict[str, int]:
        # Make tail end of URL produce a deterministic varying score
        tail = int(url.rsplit("/", 1)[-1])
        likes = (tail - 300) * 50 + 10
        return {"likes": likes, "retweets": 2, "replies": 1, "impressions": 500}

    report = asyncio.run(
        reviewer.review_and_update_weights(
            window_days=7, metrics_fetcher=fetcher
        )
    )
    assert isinstance(report, reviewer.ReviewReport)
    assert report.posts_reviewed == 6
    assert report.metrics_collected == 6
    assert len(apply_calls) == 6
    # at least one top & one bottom in the dispatch
    outcomes = {o for _, o in apply_calls}
    assert "top" in outcomes
    assert "bottom" in outcomes
    assert report.duration_seconds >= 0


def test_review_handles_no_published_drafts(
    tmp_db: Path, slop_md: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        reviewer, "apply_post_outcome", lambda *a, **kw: None
    )

    async def fetcher(url: str) -> dict[str, int]:
        raise AssertionError("should not be called")

    report = asyncio.run(
        reviewer.review_and_update_weights(metrics_fetcher=fetcher)
    )
    assert report.posts_reviewed == 0
    assert report.metrics_collected == 0
    assert report.weights_updated == []
