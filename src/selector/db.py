"""CRUD helpers for the `topic_candidates` table.

Kept intentionally thin: every function takes an open sqlite3 connection so
callers control the transaction boundary. Schema lives in
`src/migrations/004_topic_selector.sql`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = [
    "insert_candidate",
    "fetch_fresh",
    "pick_best_fresh",
    "mark_consumed",
    "expire_old_candidates",
    "VALID_LANES",
    "VALID_MODES",
    "VALID_LENGTHS",
]

VALID_LANES = frozenset(
    {
        "pre_market",
        "intraday",
        "post_market",
        "overnight",
        "general_tech_ai",
        "general_meme_career",
        "other",
    }
)
VALID_MODES = frozenset({"insight", "meme", "emotional"})
VALID_LENGTHS = frozenset({"short", "medium", "long", "article"})


def insert_candidate(
    conn: sqlite3.Connection,
    *,
    source_observation_ids: list[int],
    topic_summary: str,
    virality_score: float,
    predicted_content_mode: str,
    predicted_length: str,
    predicted_topic_lane: str,
    kol_reaction_count: int,
    emotional_intensity: float,
    debate_potential: float,
) -> int:
    """Insert a fresh candidate; returns the new row id."""
    if not source_observation_ids:
        raise ValueError("source_observation_ids must not be empty")
    if predicted_content_mode not in VALID_MODES:
        raise ValueError(f"bad content_mode: {predicted_content_mode}")
    if predicted_length not in VALID_LENGTHS:
        raise ValueError(f"bad length: {predicted_length}")
    if predicted_topic_lane not in VALID_LANES:
        raise ValueError(f"bad lane: {predicted_topic_lane}")
    if not 0 <= virality_score <= 100:
        raise ValueError("virality_score must be in [0,100]")

    cur = conn.execute(
        "INSERT INTO topic_candidates ("
        "source_observations, topic_summary, virality_score, "
        "predicted_content_mode, predicted_length, predicted_topic_lane, "
        "kol_reaction_count, emotional_intensity, debate_potential"
        ") VALUES (?,?,?,?,?,?,?,?,?)",
        (
            json.dumps(sorted(source_observation_ids)),
            topic_summary,
            float(virality_score),
            predicted_content_mode,
            predicted_length,
            predicted_topic_lane,
            int(kol_reaction_count),
            float(emotional_intensity),
            float(debate_potential),
        ),
    )
    return int(cur.lastrowid or 0)


def fetch_fresh(
    conn: sqlite3.Connection,
    *,
    topic_lane: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return fresh candidates ordered by virality_score desc."""
    sql = (
        "SELECT * FROM topic_candidates WHERE status='fresh'"
        + (" AND predicted_topic_lane=?" if topic_lane else "")
        + " ORDER BY virality_score DESC, id DESC LIMIT ?"
    )
    params: tuple[Any, ...] = (topic_lane, limit) if topic_lane else (limit,)
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def pick_best_fresh(
    conn: sqlite3.Connection, *, topic_lane: str | None = None
) -> dict[str, Any] | None:
    rows = fetch_fresh(conn, topic_lane=topic_lane, limit=1)
    return rows[0] if rows else None


def mark_consumed(
    conn: sqlite3.Connection, candidate_id: int, *, draft_id: int | None
) -> None:
    """Flip fresh -> consumed atomically. Raises if already consumed/expired."""
    cur = conn.execute(
        "UPDATE topic_candidates SET status='consumed', "
        "consumed_at=CURRENT_TIMESTAMP, consumed_by_draft_id=? "
        "WHERE id=? AND status='fresh'",
        (draft_id, candidate_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"candidate {candidate_id} not fresh (already consumed?)")


def expire_old_candidates(
    conn: sqlite3.Connection,
    *,
    older_than_hours: int = 6,
    now: datetime | None = None,
) -> int:
    """Mark fresh candidates older than N hours as expired. Returns affected rows."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=older_than_hours)
    cur = conn.execute(
        "UPDATE topic_candidates SET status='expired' "
        "WHERE status='fresh' AND generated_at < ?",
        (cutoff.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    return int(cur.rowcount)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    raw = d.get("source_observations")
    if isinstance(raw, str):
        try:
            d["source_observations"] = json.loads(raw)
        except json.JSONDecodeError:
            d["source_observations"] = []
    return d
