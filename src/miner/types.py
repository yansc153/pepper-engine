"""Frozen dataclasses for Pattern Miner I/O (shared to avoid circular imports)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

__all__ = ["TechniqueEntry", "RetrievalContext"]


@dataclass(frozen=True, slots=True)
class TechniqueEntry:
    """A distilled "why-it-went-viral" record (UNIFIED_SPEC §5.1).

    `author_handle` and `posted_at` are denormalised from the source observation
    so the weaver can compute temporal_chain edges without a second SQL hop.
    """

    id: int
    observation_id: int
    hook_pattern: str
    hook_example: str
    syntax_signature: str
    sentence_len_avg: float
    sentence_len_p90: float
    stance_strength: int
    emotion_triggers: list[str]
    image_style: str
    post_hour_utc: int
    topic_lane: str
    applicable_personas: list[str]
    content_mode: str
    optimal_length: str
    distilled_at: datetime | None
    success_score: float
    times_retrieved: int
    times_used_in_post: int
    recency_weight: float
    author_handle: str = ""
    posted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    """Writer's request for techniques (UNIFIED_SPEC §5.1)."""

    topic_lane: str
    post_hour_utc: int
    persona: str
    fact_spine_keywords: list[str]
    avoid_recent_pattern_ids: list[int]
    content_mode: str | None = None
