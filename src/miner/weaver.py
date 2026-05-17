"""Nightly + weekly weaver (UNIFIED_SPEC §6.2).

`weave_nightly` connects each new entry against the live corpus.
`weave_full` (weekly): decays recency_weight + prunes the bottom 20% of unused
entries plus their edges.
"""

from __future__ import annotations

import logging
from typing import Iterable

from src.database import get_conn, with_retry
from src.miner.db import load_entry, row_to_entry, upsert_edge
from src.miner.types import TechniqueEntry
from src.miner.weave_rules import compute_edges

__all__ = [
    "weave_nightly",
    "weave_full",
    "RECENCY_HALFLIFE_DAYS",
    "RECENCY_DECAY_FACTOR",
    "PRUNE_THRESHOLD_PERCENTILE",
    "CANDIDATE_RECENCY_FLOOR",
]

logger = logging.getLogger(__name__)

RECENCY_HALFLIFE_DAYS = 14
RECENCY_DECAY_FACTOR = 0.93
PRUNE_THRESHOLD_PERCENTILE = 20
CANDIDATE_RECENCY_FLOOR = 0.3


def _load_candidates() -> list[TechniqueEntry]:
    """Active corpus: anything with recency_weight > floor (~ last month)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT te.*, ro.author_handle, ro.posted_at "
            "FROM technique_entries te "
            "LEFT JOIN reaction_observations ro ON ro.id = te.observation_id "
            "WHERE te.recency_weight > ?",
            (CANDIDATE_RECENCY_FLOOR,),
        ).fetchall()
    finally:
        conn.close()
    return [row_to_entry(r) for r in rows]


def weave_nightly(new_entry_ids: Iterable[int]) -> int:
    """Connect each new entry to every active candidate. Returns edges created."""
    new_ids = [i for i in new_entry_ids if i >= 0]
    if not new_ids:
        return 0
    candidates = _load_candidates()
    by_id = {e.id: e for e in candidates}
    created = 0

    for new_id in new_ids:
        new_entry = by_id.get(new_id) or load_entry(new_id)
        if new_entry is None:
            continue
        for other in candidates:
            if other.id == new_entry.id:
                continue
            edges = compute_edges(new_entry, other)
            for edge_type, weight in edges:
                if upsert_edge(new_entry.id, other.id, edge_type, weight):
                    created += 1
    return created


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, int(round(pct / 100.0 * len(ordered))) - 1)
    return ordered[index]


def weave_full() -> tuple[int, int]:
    """Weekly: decay all recency_weights, prune bottom-percentile dead entries.

    Returns (decayed_count, pruned_count).
    """
    decayed = _decay_recency()
    pruned = _prune_dead_entries()
    return decayed, pruned


def _decay_recency() -> int:
    def _write() -> int:
        conn = get_conn()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE technique_entries SET recency_weight = recency_weight * ?",
                    (RECENCY_DECAY_FACTOR,),
                )
                return cur.rowcount
        finally:
            conn.close()

    return with_retry(_write)


def _prune_dead_entries() -> int:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, success_score FROM technique_entries "
            "WHERE times_used_in_post = 0"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return 0
    scores = [float(r["success_score"]) for r in rows]
    cutoff = _percentile(scores, PRUNE_THRESHOLD_PERCENTILE)
    doomed = [int(r["id"]) for r in rows if float(r["success_score"]) <= cutoff]
    if not doomed:
        return 0
    placeholders = ",".join("?" for _ in doomed)

    def _write() -> int:
        conn = get_conn()
        try:
            with conn:
                conn.execute(
                    f"DELETE FROM technique_edges WHERE src_entry_id IN ({placeholders}) "
                    f"OR dst_entry_id IN ({placeholders})",
                    tuple(doomed) * 2,
                )
                cur = conn.execute(
                    f"DELETE FROM technique_entries WHERE id IN ({placeholders})",
                    tuple(doomed),
                )
                return cur.rowcount
        finally:
            conn.close()

    return with_retry(_write)
