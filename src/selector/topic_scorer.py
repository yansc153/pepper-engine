"""Topic scorer: cluster recent observations and write topic_candidates.

Wired so it runs as a post-observe hook (S5 observer runner calls
``score_topics(conn)`` after a successful poll). The writer (S9) calls
``pick_top_topic(conn)`` to claim one candidate.

Clustering strategy (cheap on purpose):
  1. Bucket observations by author_handle so noise from one KOL collapses into
     one cluster.
  2. Greedy merge buckets whose top-content has Jaccard token overlap >= 0.35
     so cross-KOL chatter on the same topic groups together.
LLM is only invoked per cluster, not per observation.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from selector import db as selector_db
from selector.virality_predictor import (
    VirialityPredictionError,
    predict_virality,
)

__all__ = ["score_topics", "pick_top_topic", "ScoreResult"]

_JACCARD_MERGE_THRESHOLD = 0.35
_MIN_CLUSTER_SIZE = 1  # one tier-1 observation can still be a real topic
_DEFAULT_LOOKBACK_HOURS = 1
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")
# Topic-writability gate: if the LLM-predicted virality_score is below this,
# the topic is judged not worth a tweet — skip rather than push a weak draft.
_MIN_VIRALITY_TO_WRITE = 10.0  # batch_post mode: write generously, user picks in Discord


class ScoreResult(tuple[int, float]):
    """(created_count, top_score) — tuple subclass for readability in callers."""

    def __new__(cls, created: int, top: float) -> "ScoreResult":
        return super().__new__(cls, (created, top))

    @property
    def created(self) -> int:
        return self[0]

    @property
    def top_score(self) -> float:
        return self[1]


def score_topics(
    conn: sqlite3.Connection,
    *,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
    now: datetime | None = None,
    miner_retrieve: Callable[..., list[dict[str, Any]]] | None = None,
    llm_caller: Any = None,
) -> ScoreResult:
    """Cluster recent observations + score each + insert fresh candidates.

    Returns (created_count, top_score). top_score is 0.0 when nothing inserted.
    """
    observations = _load_recent_observations(conn, lookback_hours=lookback_hours, now=now)
    if not observations:
        return ScoreResult(0, 0.0)

    clusters = _cluster_observations(observations)
    created = 0
    top = 0.0

    for cluster in clusters:
        if len(cluster) < _MIN_CLUSTER_SIZE:
            continue
        patterns = _safe_retrieve(miner_retrieve, cluster)
        try:
            prediction = predict_virality(
                cluster, historical_patterns=patterns, llm_caller=llm_caller
            )
        except VirialityPredictionError:
            continue

        ids = [int(o["id"]) for o in cluster if o.get("id") is not None]
        if not ids:
            continue

        with conn:
            selector_db.insert_candidate(
                conn,
                source_observation_ids=ids,
                **{k: prediction[k] for k in (
                    "topic_summary",
                    "virality_score",
                    "predicted_content_mode",
                    "predicted_length",
                    "predicted_topic_lane",
                    "kol_reaction_count",
                    "emotional_intensity",
                    "debate_potential",
                )},
            )
        created += 1
        top = max(top, float(prediction["virality_score"]))

    return ScoreResult(created, top)


def pick_top_topic(
    conn: sqlite3.Connection,
    *,
    draft_id: int | None = None,
    topic_lane: str | None = None,
) -> dict[str, Any] | None:
    """Claim the best lane-weighted fresh candidate; mark consumed atomically.

    Applies strategy_weights as a multiplier so reviewer feedback influences
    which lane gets written next.
    """
    weight_rows = conn.execute(
        "SELECT topic_lane, weight FROM strategy_weights"
    ).fetchall()
    weights: dict[str, float] = {r["topic_lane"]: float(r["weight"]) for r in weight_rows}

    candidates = selector_db.fetch_fresh(conn, topic_lane=topic_lane, limit=20)
    if not candidates:
        return None

    # LLM-judged writability gate: drop candidates the predictor scored too low.
    # virality_predictor already runs an LLM call during score_topics — we honor
    # its judgment here rather than burning a second LLM call.
    candidates = [c for c in candidates if float(c["virality_score"]) >= _MIN_VIRALITY_TO_WRITE]
    if not candidates:
        return None

    def _weighted(c: dict[str, Any]) -> float:
        w = weights.get(c.get("predicted_topic_lane", ""), 1.0)
        return float(c["virality_score"]) * max(0.1, w)

    best = max(candidates, key=_weighted)
    with conn:
        selector_db.mark_consumed(conn, int(best["id"]), draft_id=draft_id)
    best["status"] = "consumed"
    best["consumed_by_draft_id"] = draft_id
    return best


# ---------------------------------------------------------------------------
# internals


def _load_recent_observations(
    conn: sqlite3.Connection,
    *,
    lookback_hours: int,
    now: datetime | None,
) -> list[dict[str, Any]]:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=lookback_hours)
    rows = conn.execute(
        "SELECT id, source, author_handle, author_tier, content, posted_at, "
        "likes, retweets, replies, impressions, has_image, raw_url, topic_hint, "
        "viral_score FROM reaction_observations "
        "WHERE observed_at >= ? AND author_tier >= 0 "
        # tier>=0 (was >0): xueqiu/futu now ship tier=0 so they contribute
        # topic candidates without polluting the technique-learning corpus
        # (distiller still filters tier>0 for shape extraction).
        "ORDER BY observed_at DESC",
        (cutoff.strftime("%Y-%m-%d %H:%M:%S"),),
    ).fetchall()
    return [dict(r) for r in rows]


def _cluster_observations(
    observations: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Bucket by handle, then greedy-merge by Jaccard token overlap."""
    by_handle: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        by_handle.setdefault(str(obs.get("author_handle", "")), []).append(obs)

    buckets = list(by_handle.values())
    merged: list[list[dict[str, Any]]] = []
    while buckets:
        head = buckets.pop(0)
        head_tokens = _bucket_tokens(head)
        absorbed = []
        for i, other in enumerate(buckets):
            if _jaccard(head_tokens, _bucket_tokens(other)) >= _JACCARD_MERGE_THRESHOLD:
                head.extend(other)
                absorbed.append(i)
        for i in reversed(absorbed):
            buckets.pop(i)
        merged.append(head)
    return merged


def _bucket_tokens(bucket: list[dict[str, Any]]) -> set[str]:
    tokens: set[str] = set()
    for obs in bucket:
        tokens.update(_tokenize(obs.get("content") or ""))
    return tokens


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text) if len(t) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _safe_retrieve(
    miner_retrieve: Callable[..., list[dict[str, Any]]] | None,
    cluster: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if miner_retrieve is None:
        return []
    try:
        lane = cluster[0].get("topic_hint") or "other"
        return list(miner_retrieve(topic_lane=lane) or [])
    except Exception:  # noqa: BLE001 — miner is optional context
        return []
