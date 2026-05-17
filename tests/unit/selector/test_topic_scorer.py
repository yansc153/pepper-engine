"""Tests for selector.topic_scorer."""
from __future__ import annotations

import json
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
from selector.topic_scorer import (  # noqa: E402
    ScoreResult,
    pick_top_topic,
    score_topics,
)


_PAYLOAD = {
    "virality_score": 77,
    "predicted_content_mode": "insight",
    "predicted_length": "medium",
    "predicted_topic_lane": "pre_market",
    "kol_reaction_count": 0,
    "emotional_intensity": 0.5,
    "debate_potential": 0.5,
    "topic_summary": "纳指夜跌引发分歧",
    "reasoning": "多位 tier-1 在讨论",
}


@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "x.db"
    init_db(db)
    c = get_conn(db)
    yield c
    c.close()


def _insert_obs(
    conn,
    *,
    handle: str = "alpha",
    tier: int = 1,
    content: str = "纳指 跌 4% 夜盘",
    minutes_ago: int = 5,
    raw_url: str | None = None,
) -> int:
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    posted_at = observed_at
    cur = conn.execute(
        "INSERT INTO reaction_observations ("
        "source, author_handle, author_tier, content, posted_at, "
        "likes, retweets, replies, impressions, has_image, raw_url, "
        "topic_hint, viral_score, is_viral, observed_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "x_list_finance",
            handle,
            tier,
            content,
            posted_at.strftime("%Y-%m-%d %H:%M:%S"),
            100,
            10,
            20,
            None,
            0,
            raw_url or f"https://x.com/{handle}/{minutes_ago}",
            "pre_market",
            0.5,
            0,
            observed_at.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _llm_returning(payload):
    def _fn(prompt, **kwargs):
        return json.dumps(payload)
    return _fn


def test_score_topics_returns_zero_when_no_observations(conn):
    result = score_topics(conn, llm_caller=_llm_returning(_PAYLOAD))
    assert isinstance(result, ScoreResult)
    assert result.created == 0
    assert result.top_score == 0.0


def test_score_topics_ignores_tier_zero_observations(conn):
    _insert_obs(conn, tier=0, handle="news_bot")
    result = score_topics(conn, llm_caller=_llm_returning(_PAYLOAD))
    assert result.created == 0


def test_score_topics_creates_candidate_for_tier1(conn):
    _insert_obs(conn, handle="alpha", tier=1)
    result = score_topics(conn, llm_caller=_llm_returning(_PAYLOAD))
    assert result.created == 1
    assert result.top_score == 77.0
    rows = selector_db.fetch_fresh(conn)
    assert len(rows) == 1
    assert rows[0]["topic_summary"] == "纳指夜跌引发分歧"


def test_score_topics_clusters_by_jaccard_overlap(conn):
    # Two different KOLs talking about the same topic -> one cluster
    _insert_obs(
        conn, handle="alpha", tier=1, content="NVDA earnings beat consensus tonight"
    )
    _insert_obs(
        conn,
        handle="beta",
        tier=1,
        content="NVDA earnings beat consensus blowout",
        minutes_ago=8,
    )
    result = score_topics(conn, llm_caller=_llm_returning(_PAYLOAD))
    assert result.created == 1  # merged into one cluster


def test_score_topics_keeps_unrelated_topics_separate(conn):
    _insert_obs(conn, handle="alpha", tier=1, content="NVDA earnings beat consensus")
    _insert_obs(
        conn,
        handle="beta",
        tier=1,
        content="AAPL services revenue record quarter",
        minutes_ago=10,
    )
    result = score_topics(conn, llm_caller=_llm_returning(_PAYLOAD))
    assert result.created == 2


def test_score_topics_skips_observations_outside_lookback(conn):
    _insert_obs(conn, handle="alpha", tier=1, minutes_ago=240)  # 4h ago
    result = score_topics(
        conn, lookback_hours=1, llm_caller=_llm_returning(_PAYLOAD)
    )
    assert result.created == 0


def test_score_topics_passes_miner_patterns_to_llm(conn):
    _insert_obs(conn, handle="alpha", tier=1)
    seen = {}

    def cap_llm(prompt, **kwargs):
        seen["prompt"] = prompt
        return json.dumps(_PAYLOAD)

    def fake_retrieve(**kwargs):
        return [{"hook_pattern": "反差对比", "success_score": 91, "topic_lane": "pre_market"}]

    score_topics(conn, llm_caller=cap_llm, miner_retrieve=fake_retrieve)
    assert "反差对比" in seen["prompt"]


def test_pick_top_topic_returns_none_when_no_fresh(conn):
    assert pick_top_topic(conn) is None


def test_pick_top_topic_claims_highest_score_and_marks_consumed(conn):
    _insert_obs(conn, handle="alpha", tier=1)
    score_topics(
        conn,
        llm_caller=_llm_returning(dict(_PAYLOAD, virality_score=95)),
    )
    picked = pick_top_topic(conn)
    assert picked is not None
    assert picked["status"] == "consumed"
    assert picked["virality_score"] == 95.0
    # second call sees no fresh
    assert pick_top_topic(conn) is None


def test_pick_top_topic_filters_by_lane(conn):
    _insert_obs(conn, handle="alpha", tier=1)
    score_topics(conn, llm_caller=_llm_returning(_PAYLOAD))
    # cluster was inserted with lane=pre_market
    assert pick_top_topic(conn, topic_lane="overnight") is None
    assert pick_top_topic(conn, topic_lane="pre_market") is not None


def test_pick_top_topic_honors_strategy_weights(conn):
    """reviewer→weights→selector feedback loop must actually steer selection."""
    # Insert two clusters: pre_market (score 80) and post_market (score 70)
    _insert_obs(conn, handle="alpha", tier=1)
    score_topics(
        conn,
        llm_caller=_llm_returning(dict(_PAYLOAD, virality_score=80, predicted_topic_lane="pre_market")),
    )
    _insert_obs(conn, handle="beta", tier=1, content="尾盘 跳水")
    score_topics(
        conn,
        llm_caller=_llm_returning(dict(_PAYLOAD, virality_score=70, predicted_topic_lane="post_market")),
    )

    # Without weights → 80 wins
    picked = pick_top_topic(conn)
    assert picked["predicted_topic_lane"] == "pre_market"

    # Reset to fresh state
    conn.execute("UPDATE topic_candidates SET status='fresh', consumed_at=NULL")

    # Heavy weight on post_market (3.0) flips the choice (3.0*70=210 > 1.0*80=80)
    with conn:
        conn.execute(
            "INSERT INTO strategy_weights (topic_lane, weight, reason) VALUES "
            "('pre_market', 1.0, 'test'), ('post_market', 3.0, 'test')"
        )
    picked = pick_top_topic(conn)
    assert picked["predicted_topic_lane"] == "post_market", (
        "strategy_weights must steer pick_top_topic — reviewer feedback loop"
    )
