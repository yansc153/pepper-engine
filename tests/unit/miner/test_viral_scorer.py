"""Unit tests for src.miner.viral_scorer."""

from __future__ import annotations

from pathlib import Path

from src.database import get_conn
from src.miner import viral_scorer as vs


def test_viral_score_no_impressions_uses_only_engagement() -> None:
    # 10*0.5 + 5*1 + 2*27 = 64
    assert vs.viral_score(10, 5, 2, None) == 64.0


def test_viral_score_with_impressions_adds_profile_clicks_and_negfb() -> None:
    score = vs.viral_score(0, 0, 0, 10000)
    # profile_clicks_est = 200 * 12 = 2400; negfb_est = 10 * -74 = -740
    assert score == 2400 - 740


def test_viral_score_clamps_negative_inputs_to_zero() -> None:
    assert vs.viral_score(-100, -10, -1, None) == 0.0


def test_is_viral_strict_inequality() -> None:
    assert vs.is_viral(50.001, 50.0) is True
    assert vs.is_viral(50.0, 50.0) is False  # exactly equal → not viral
    assert vs.is_viral(49.99, 50.0) is False


def test_author_p80_fallback_when_few_samples(tmp_db: Path) -> None:
    # No observations at all → DEFAULT
    assert vs.author_p80("ghost") == vs.DEFAULT_AUTHOR_P80


def test_author_p80_computes_from_history(tmp_db: Path) -> None:
    conn = get_conn(tmp_db)
    try:
        with conn:
            for i, score in enumerate([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], start=1):
                conn.execute(
                    "INSERT INTO reaction_observations (id, source, author_handle, "
                    "author_tier, content, posted_at, likes, retweets, replies, "
                    "has_image, raw_url, viral_score, is_viral) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        i,
                        "x_list_finance",
                        "alice",
                        1,
                        f"c{i}",
                        "2026-05-01 00:00:00",
                        100,
                        0,
                        0,
                        0,
                        f"https://example.com/{i}",
                        float(score),
                        1,
                    ),
                )
    finally:
        conn.close()
    # p80 of [10..100] nearest-rank index = round(0.8*10)-1 = 7 → 80
    assert vs.author_p80("alice") == 80.0
