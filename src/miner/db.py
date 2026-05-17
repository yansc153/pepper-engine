"""DB helpers for Pattern Miner — wraps `src.database.get_conn` only.

All writes go through `with_retry` so a transient SQLITE_BUSY doesn't poison the
nightly cron. Static fallback entries use negative ids; we never persist them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.database import get_conn, with_retry
from src.miner.types import RetrievalContext, TechniqueEntry

__all__ = [
    "load_entry",
    "upsert_entry",
    "upsert_edge",
    "log_retrieval",
    "increment_times_retrieved",
    "row_to_entry",
    "context_signature",
]


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def row_to_entry(row: Any) -> TechniqueEntry:
    """Decode a `technique_entries` row joined with author/posted_at if present."""
    keys = row.keys() if hasattr(row, "keys") else list(row)
    has = keys.__contains__

    def get(k: str, default: Any = None) -> Any:
        if has(k):
            return row[k]
        return default

    return TechniqueEntry(
        id=int(row["id"]),
        observation_id=int(row["observation_id"]),
        hook_pattern=row["hook_pattern"],
        hook_example=row["hook_example"],
        syntax_signature=row["syntax_signature"],
        sentence_len_avg=float(row["sentence_len_avg"]),
        sentence_len_p90=float(row["sentence_len_p90"]),
        stance_strength=int(row["stance_strength"]),
        emotion_triggers=json.loads(row["emotion_triggers"] or "[]"),
        image_style=row["image_style"],
        post_hour_utc=int(row["post_hour_utc"]),
        topic_lane=row["topic_lane"],
        applicable_personas=json.loads(row["applicable_personas"] or "[]"),
        content_mode=row["content_mode"],
        optimal_length=row["optimal_length"],
        distilled_at=_parse_dt(row["distilled_at"]),
        success_score=float(row["success_score"]),
        times_retrieved=int(row["times_retrieved"] or 0),
        times_used_in_post=int(row["times_used_in_post"] or 0),
        recency_weight=float(row["recency_weight"] or 1.0),
        author_handle=str(get("author_handle", "") or ""),
        posted_at=_parse_dt(get("posted_at")),
    )


def load_entry(entry_id: int) -> TechniqueEntry | None:
    """Fetch a single entry joined with its source observation; None if missing."""
    if entry_id < 0:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT te.*, ro.author_handle, ro.posted_at "
            "FROM technique_entries te "
            "LEFT JOIN reaction_observations ro ON ro.id = te.observation_id "
            "WHERE te.id = ?",
            (entry_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row_to_entry(row)


def upsert_entry(observation_id: int, fields: dict[str, Any]) -> int:
    """Insert/replace a TechniqueEntry; returns its row id.

    Idempotent via UNIQUE(observation_id) — duplicates UPDATE in place rather
    than throwing.
    """
    payload = {
        "observation_id": int(observation_id),
        "hook_pattern": fields["hook_pattern"],
        "hook_example": fields["hook_example"],
        "syntax_signature": fields["syntax_signature"],
        "sentence_len_avg": float(fields.get("sentence_len_avg", 0)),
        "sentence_len_p90": float(fields.get("sentence_len_p90", 0)),
        "stance_strength": int(fields.get("stance_strength", 0)),
        "emotion_triggers": json.dumps(
            fields.get("emotion_triggers", []), ensure_ascii=False
        ),
        "image_style": fields.get("image_style", "none"),
        "post_hour_utc": int(fields.get("post_hour_utc", 0)),
        "topic_lane": fields["topic_lane"],
        "applicable_personas": json.dumps(
            fields.get("applicable_personas", []), ensure_ascii=False
        ),
        "content_mode": fields.get("content_mode", "insight"),
        "optimal_length": fields.get("optimal_length", "short"),
        "success_score": float(fields.get("success_score", 50.0)),
        "recency_weight": float(fields.get("recency_weight", 1.0)),
    }

    def _write() -> int:
        conn = get_conn()
        try:
            with conn:
                existing = conn.execute(
                    "SELECT id FROM technique_entries WHERE observation_id = ?",
                    (payload["observation_id"],),
                ).fetchone()
                if existing is not None:
                    conn.execute(
                        "UPDATE technique_entries SET "
                        "hook_pattern=:hook_pattern, hook_example=:hook_example, "
                        "syntax_signature=:syntax_signature, "
                        "sentence_len_avg=:sentence_len_avg, "
                        "sentence_len_p90=:sentence_len_p90, "
                        "stance_strength=:stance_strength, "
                        "emotion_triggers=:emotion_triggers, "
                        "image_style=:image_style, post_hour_utc=:post_hour_utc, "
                        "topic_lane=:topic_lane, "
                        "applicable_personas=:applicable_personas, "
                        "content_mode=:content_mode, "
                        "optimal_length=:optimal_length, "
                        "distilled_at=CURRENT_TIMESTAMP "
                        "WHERE observation_id=:observation_id",
                        payload,
                    )
                    return int(existing["id"])
                cur = conn.execute(
                    "INSERT INTO technique_entries "
                    "(observation_id, hook_pattern, hook_example, syntax_signature, "
                    "sentence_len_avg, sentence_len_p90, stance_strength, "
                    "emotion_triggers, image_style, post_hour_utc, topic_lane, "
                    "applicable_personas, content_mode, optimal_length, "
                    "success_score, recency_weight) "
                    "VALUES (:observation_id, :hook_pattern, :hook_example, "
                    ":syntax_signature, :sentence_len_avg, :sentence_len_p90, "
                    ":stance_strength, :emotion_triggers, :image_style, "
                    ":post_hour_utc, :topic_lane, :applicable_personas, "
                    ":content_mode, :optimal_length, :success_score, "
                    ":recency_weight)",
                    payload,
                )
                return int(cur.lastrowid or 0)
        finally:
            conn.close()

    return with_retry(_write)


def upsert_edge(src: int, dst: int, edge_type: str, weight: float) -> bool:
    """Insert an undirected edge (CHECK src<dst). True if inserted, False if dup."""
    if src == dst:
        return False
    lo, hi = (src, dst) if src < dst else (dst, src)

    def _write() -> bool:
        conn = get_conn()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO technique_edges "
                    "(src_entry_id, dst_entry_id, edge_type, weight) "
                    "VALUES (?, ?, ?, ?)",
                    (lo, hi, edge_type, float(weight)),
                )
                return cur.rowcount > 0
        finally:
            conn.close()

    return with_retry(_write)


def context_signature(ctx: RetrievalContext) -> str:
    """Stable signature for retrieval_log dedup / inspection."""
    payload = {
        "lane": ctx.topic_lane,
        "hour": ctx.post_hour_utc,
        "persona": ctx.persona,
        "mode": ctx.content_mode,
        "kw": sorted(ctx.fact_spine_keywords or []),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def log_retrieval(ctx: RetrievalContext, ids: list[int]) -> None:
    """Append a retrieval audit row. post_id is null until writer publishes."""

    def _write() -> None:
        conn = get_conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO retrieval_log (post_id, retrieved_entry_ids, "
                    "context_signature) VALUES (NULL, ?, ?)",
                    (json.dumps(ids), context_signature(ctx)),
                )
        finally:
            conn.close()

    with_retry(_write)


def increment_times_retrieved(ids: list[int]) -> None:
    """Bump counter for every retrieved entry (negative ids = static, skip)."""
    positives = [i for i in ids if i >= 0]
    if not positives:
        return
    placeholders = ",".join("?" for _ in positives)

    def _write() -> None:
        conn = get_conn()
        try:
            with conn:
                conn.execute(
                    f"UPDATE technique_entries SET times_retrieved = times_retrieved + 1 "
                    f"WHERE id IN ({placeholders})",
                    tuple(positives),
                )
        finally:
            conn.close()

    with_retry(_write)
