"""Viral score formula (single source of truth).

Aligned with Twitter's open-sourced ranking heavy hitters (see UNIFIED_SPEC §5.3
+ Appendix B). Profile clicks and negative feedback aren't directly observable
on scraped posts, so we estimate them from impressions when available.
"""

from __future__ import annotations

from src.database import get_conn

__all__ = [
    "WEIGHT_LIKE",
    "WEIGHT_RETWEET",
    "WEIGHT_REPLY",
    "WEIGHT_PROFILE_CLICK",
    "WEIGHT_NEGATIVE_FEEDBACK",
    "PROFILE_CLICK_RATE_ESTIMATE",
    "NEGATIVE_FEEDBACK_RATE_ESTIMATE",
    "DEFAULT_AUTHOR_P80",
    "viral_score",
    "is_viral",
    "author_p80",
]

WEIGHT_LIKE = 0.5
WEIGHT_RETWEET = 1.0
WEIGHT_REPLY = 27.0
WEIGHT_PROFILE_CLICK = 12.0
WEIGHT_NEGATIVE_FEEDBACK = -74.0

PROFILE_CLICK_RATE_ESTIMATE = 0.02
NEGATIVE_FEEDBACK_RATE_ESTIMATE = 0.001

DEFAULT_AUTHOR_P80 = 50.0
_MIN_SAMPLES_FOR_P80 = 5


def viral_score(
    likes: int,
    retweets: int,
    replies: int,
    impressions: int | None,
) -> float:
    """Weighted blend that mirrors X's ranking heuristic."""
    base = (
        WEIGHT_LIKE * max(0, likes)
        + WEIGHT_RETWEET * max(0, retweets)
        + WEIGHT_REPLY * max(0, replies)
    )
    if impressions and impressions > 0:
        profile_clicks_est = impressions * PROFILE_CLICK_RATE_ESTIMATE
        neg_fb_est = impressions * NEGATIVE_FEEDBACK_RATE_ESTIMATE
        base += WEIGHT_PROFILE_CLICK * profile_clicks_est
        base += WEIGHT_NEGATIVE_FEEDBACK * neg_fb_est
    return float(base)


def is_viral(score: float, p80: float) -> bool:
    """Strictly greater than p80; equality is NOT viral."""
    return score > p80


def author_p80(author_handle: str, window_days: int = 30) -> float:
    """80th percentile of an author's recent viral_scores.

    Falls back to DEFAULT_AUTHOR_P80 when fewer than 5 samples exist.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT viral_score FROM reaction_observations "
            "WHERE author_handle = ? "
            "AND observed_at >= datetime('now', ?) "
            "ORDER BY viral_score ASC",
            (author_handle, f"-{int(window_days)} days"),
        ).fetchall()
    finally:
        conn.close()
    scores = [float(r["viral_score"]) for r in rows]
    if len(scores) < _MIN_SAMPLES_FOR_P80:
        return DEFAULT_AUTHOR_P80
    # nearest-rank p80
    index = max(0, int(round(0.8 * len(scores))) - 1)
    return scores[index]
