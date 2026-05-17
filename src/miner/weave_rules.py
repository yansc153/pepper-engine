"""Pure functions describing the 5 technique-edge types.

Kept side-effect-free so the weaver can unit-test edge computation without
touching the database.
"""

from __future__ import annotations

from typing import Literal

from src.miner.types import TechniqueEntry

__all__ = [
    "EdgeType",
    "FINANCE_LANES",
    "GENERAL_LANES",
    "compute_edges",
    "iou",
    "is_cross_domain",
]

EdgeType = Literal[
    "same_hook",
    "same_lane_diff_angle",
    "co_occurring_emotion",
    "temporal_chain",
    "cross_domain_bridge",
]

FINANCE_LANES: frozenset[str] = frozenset(
    {"pre_market", "intraday", "post_market", "overnight"}
)
GENERAL_LANES: frozenset[str] = frozenset({"general_tech_ai", "general_meme_career"})

_TEMPORAL_CHAIN_HOURS = 48
_CO_OCCURRING_THRESHOLD = 0.5


def iou(a: list[str] | None, b: list[str] | None) -> float:
    """Intersection-over-union over two emotion lists.

    Both empty / either empty → 0.0 (no signal).
    """
    if not a or not b:
        return 0.0
    set_a = {x for x in a if x}
    set_b = {x for x in b if x}
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return inter / union


def is_cross_domain(lane_a: str, lane_b: str) -> bool:
    """True iff one lane is finance and the other is general (or vice versa)."""
    a_fin = lane_a in FINANCE_LANES
    b_fin = lane_b in FINANCE_LANES
    a_gen = lane_a in GENERAL_LANES
    b_gen = lane_b in GENERAL_LANES
    return (a_fin and b_gen) or (a_gen and b_fin)


def compute_edges(
    a: TechniqueEntry,
    b: TechniqueEntry,
) -> list[tuple[EdgeType, float]]:
    """Return every edge that should connect a→b (a.id < b.id assumed by caller)."""
    edges: list[tuple[EdgeType, float]] = []

    if a.hook_pattern == b.hook_pattern:
        edges.append(("same_hook", 1.0))
    if a.topic_lane == b.topic_lane and a.hook_pattern != b.hook_pattern:
        edges.append(("same_lane_diff_angle", 0.7))

    emotion_iou = iou(a.emotion_triggers, b.emotion_triggers)
    if emotion_iou > _CO_OCCURRING_THRESHOLD:
        edges.append(("co_occurring_emotion", float(emotion_iou)))

    if a.author_handle and a.author_handle == b.author_handle:
        if a.posted_at and b.posted_at:
            diff_hours = abs((a.posted_at - b.posted_at).total_seconds()) / 3600.0
            if diff_hours < _TEMPORAL_CHAIN_HOURS:
                edges.append(("temporal_chain", 0.8))

    if (
        is_cross_domain(a.topic_lane, b.topic_lane)
        and a.syntax_signature == b.syntax_signature
    ):
        edges.append(("cross_domain_bridge", 0.9))

    return edges
