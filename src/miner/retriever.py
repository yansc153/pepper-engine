"""Top-K technique retriever (UNIFIED_SPEC §6.3, §6.4).

Tiered cold-start:
- Day 0-3 / corpus < 50  → 100% static fallback (in-memory dict filter)
- Day 4-14 / 50-100      → 50/50 mix
- Day 15-30 / >100       → 100% SQL, recency-first
- Day 30+ / steady state → success_score × recency_weight (the SQL block below)

Performance target: < 200ms over a 500-entry fixture (covered in tests).
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from src.database import get_conn
from src.miner.db import (
    increment_times_retrieved,
    log_retrieval,
    row_to_entry,
)
from src.miner.static_fallback import filter_static, load_static_entries
from src.miner.types import RetrievalContext, TechniqueEntry

__all__ = [
    "retrieve",
    "TOP_K_TECHNIQUES",
    "BRIDGE_QUOTA_RATIO",
    "HOUR_WINDOW",
    "COLD_START_CORPUS_THRESHOLD",
    "WARM_START_CORPUS_THRESHOLD",
]

logger = logging.getLogger(__name__)

TOP_K_TECHNIQUES = 5
BRIDGE_QUOTA_RATIO = 0.1
HOUR_WINDOW = 2
COLD_START_CORPUS_THRESHOLD = 50
WARM_START_CORPUS_THRESHOLD = 100


def _corpus_size() -> int:
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM technique_entries").fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


def _sql_retrieve(ctx: RetrievalContext, k: int) -> list[TechniqueEntry]:
    """Main SQL pass (lane + bridge), already cooling-aware."""
    k_bridge = max(1, math.ceil(k * BRIDGE_QUOTA_RATIO))
    k_main = max(1, k - k_bridge)
    avoid_json = json.dumps(list(ctx.avoid_recent_pattern_ids or []))

    # NB: the spec SQL uses json_each() on applicable_personas; we keep the same
    # shape but materialise lane_hits via a temp expression so we can join twice.
    sql_lane = """
        SELECT te.*, ro.author_handle, ro.posted_at,
               te.success_score * te.recency_weight AS _rank
        FROM technique_entries te
        LEFT JOIN reaction_observations ro ON ro.id = te.observation_id
        WHERE te.topic_lane = :lane
          AND ABS(te.post_hour_utc - :hour) <= :win
          AND EXISTS (
              SELECT 1 FROM json_each(te.applicable_personas)
              WHERE json_each.value = :persona
          )
          AND te.id NOT IN (SELECT value FROM json_each(:avoid))
          AND te.id NOT IN (
              SELECT pattern_id FROM pattern_cooling
              WHERE reset_after IS NULL OR reset_after > CURRENT_TIMESTAMP
          )
        ORDER BY _rank DESC
        LIMIT :limit
    """

    sql_bridge = """
        SELECT te.*, ro.author_handle, ro.posted_at,
               te.success_score * te.recency_weight * 0.7 AS _rank
        FROM technique_edges ed
        JOIN technique_entries te ON te.id = ed.dst_entry_id
        LEFT JOIN reaction_observations ro ON ro.id = te.observation_id
        WHERE ed.edge_type = 'cross_domain_bridge'
          AND ed.src_entry_id IN (
              SELECT id FROM technique_entries
              WHERE topic_lane = :lane
                AND ABS(post_hour_utc - :hour) <= :win
          )
          AND te.id NOT IN (SELECT value FROM json_each(:avoid))
          AND te.id NOT IN (
              SELECT pattern_id FROM pattern_cooling
              WHERE reset_after IS NULL OR reset_after > CURRENT_TIMESTAMP
          )
        ORDER BY _rank DESC
        LIMIT :limit
    """

    params: dict[str, Any] = {
        "lane": ctx.topic_lane,
        "hour": ctx.post_hour_utc,
        "win": HOUR_WINDOW,
        "persona": ctx.persona,
        "avoid": avoid_json,
    }

    conn = get_conn()
    try:
        main_rows = conn.execute(sql_lane, {**params, "limit": k_main}).fetchall()
        bridge_rows = conn.execute(
            sql_bridge, {**params, "limit": k_bridge}
        ).fetchall()
    finally:
        conn.close()

    seen: set[int] = set()
    results: list[TechniqueEntry] = []
    for row in list(main_rows) + list(bridge_rows):
        entry = row_to_entry(row)
        if entry.id in seen:
            continue
        seen.add(entry.id)
        results.append(entry)
        if len(results) >= k:
            break
    return results


def _static_retrieve(ctx: RetrievalContext, k: int) -> list[TechniqueEntry]:
    entries = load_static_entries()
    avoid = set(ctx.avoid_recent_pattern_ids or [])
    return filter_static(
        entries,
        topic_lane=ctx.topic_lane,
        post_hour_utc=ctx.post_hour_utc,
        persona=ctx.persona,
        avoid=avoid,
        hour_window=HOUR_WINDOW,
        k=k,
    )


def _merge(
    primary: list[TechniqueEntry],
    secondary: list[TechniqueEntry],
    k: int,
) -> list[TechniqueEntry]:
    """De-dup by id, primary first."""
    out: list[TechniqueEntry] = []
    seen: set[int] = set()
    for e in primary + secondary:
        if e.id in seen:
            continue
        seen.add(e.id)
        out.append(e)
        if len(out) >= k:
            break
    return out


def retrieve(
    ctx: RetrievalContext,
    k: int = TOP_K_TECHNIQUES,
) -> list[TechniqueEntry]:
    """Return up to k TechniqueEntry rows ranked by success × recency.

    Mixes SQL hits with the static fallback corpus per cold-start tier so the
    writer always has *something* to anchor on.
    """
    if k <= 0:
        return []

    size = _corpus_size()
    if size < COLD_START_CORPUS_THRESHOLD:
        results = _static_retrieve(ctx, k)
    elif size < WARM_START_CORPUS_THRESHOLD:
        half = max(1, k // 2)
        sql_part = _sql_retrieve(ctx, half)
        static_part = _static_retrieve(ctx, k - len(sql_part))
        results = _merge(sql_part, static_part, k)
    else:
        results = _sql_retrieve(ctx, k)
        if not results:
            results = _static_retrieve(ctx, k)

    # Best-effort audit — never block retrieval on logging hiccups.
    try:
        ids = [e.id for e in results]
        log_retrieval(ctx, ids)
        increment_times_retrieved(ids)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("retrieval audit failed: %s", exc)

    return results
