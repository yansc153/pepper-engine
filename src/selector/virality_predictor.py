"""Virality predictor — wraps the LLM call + post-processing for one topic cluster.

Inputs are pre-clustered observations (list of dict rows) plus optional historical
patterns retrieved by S6.miner. The LLM returns a JSON envelope; we trust LLM
labels but clamp numeric ranges and recompute a few signals deterministically
from the raw engagement (so the LLM cannot inflate kol_reaction_count).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from llm import LLMError, call_llm
from selector.db import VALID_LANES, VALID_LENGTHS, VALID_MODES

__all__ = ["predict_virality", "VirialityPredictionError", "PROMPT_PATH"]

PROMPT_PATH = Path(__file__).parent / "prompts" / "score_topic.txt"

_MAX_CLUSTER_PREVIEW = 12  # cap observations sent to LLM to keep prompt small


class VirialityPredictionError(RuntimeError):
    """Raised when the LLM response cannot be coerced into a valid prediction."""


def predict_virality(
    observations: list[dict[str, Any]],
    *,
    historical_patterns: list[dict[str, Any]] | None = None,
    llm_caller: Any = None,
) -> dict[str, Any]:
    """Score a single topic cluster.

    Args:
        observations: rows from `reaction_observations` (dict-like).
        historical_patterns: optional miner.retrieve() result for context.
        llm_caller: override for `llm.call_llm` (tests inject here).

    Returns a dict matching the topic_candidates insert schema.
    """
    if not observations:
        raise VirialityPredictionError("empty observation cluster")

    caller = llm_caller or call_llm
    prompt = _render_prompt(observations, historical_patterns or [])

    try:
        raw = caller(prompt, response_format="json", max_retries=2, timeout=90)
    except LLMError as exc:
        raise VirialityPredictionError(f"llm call failed: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VirialityPredictionError(f"llm returned non-JSON: {raw!r}") from exc

    return _coerce_payload(payload, observations)


def _render_prompt(
    observations: list[dict[str, Any]], patterns: list[dict[str, Any]]
) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    cluster = _format_cluster(observations[:_MAX_CLUSTER_PREVIEW])
    pattern_block = _format_patterns(patterns) or "(none — cold start)"
    return template.replace("{cluster_block}", cluster).replace(
        "{pattern_block}", pattern_block
    )


def _format_cluster(observations: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for obs in observations:
        lines.append(
            f"- @{obs.get('author_handle')} (tier={obs.get('author_tier')}, "
            f"likes={obs.get('likes', 0)}, replies={obs.get('replies', 0)}): "
            f"{(obs.get('content') or '').strip()[:240]}"
        )
    return "\n".join(lines)


def _format_patterns(patterns: list[dict[str, Any]]) -> str:
    if not patterns:
        return ""
    return "\n".join(
        f"- pattern={p.get('hook_pattern')!r} "
        f"success_score={p.get('success_score')} "
        f"lane={p.get('topic_lane')}"
        for p in patterns[:5]
    )


def _coerce_payload(
    payload: dict[str, Any], observations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Clamp + validate the LLM payload; recompute kol_reaction_count from raw obs."""
    if not isinstance(payload, dict):
        raise VirialityPredictionError(f"payload not a dict: {payload!r}")

    mode = payload.get("predicted_content_mode")
    length = payload.get("predicted_length")
    lane = payload.get("predicted_topic_lane")
    if mode not in VALID_MODES:
        raise VirialityPredictionError(f"bad content_mode: {mode!r}")
    if length not in VALID_LENGTHS:
        raise VirialityPredictionError(f"bad length: {length!r}")
    if lane not in VALID_LANES:
        raise VirialityPredictionError(f"bad lane: {lane!r}")

    summary = str(payload.get("topic_summary") or "").strip()
    if not summary:
        raise VirialityPredictionError("topic_summary missing")

    # Trust LLM scoring but clamp; recompute tier-1 KOL count from raw observations
    # so the LLM cannot fabricate engagement.
    score = _clamp_number(payload.get("virality_score"), 0, 100, default=0.0)
    emo = _clamp_number(payload.get("emotional_intensity"), 0, 1, default=0.0)
    debate = _clamp_number(payload.get("debate_potential"), 0, 1, default=0.0)
    tier1_count = sum(1 for o in observations if int(o.get("author_tier", 9)) <= 1)

    return {
        "virality_score": float(score),
        "predicted_content_mode": mode,
        "predicted_length": length,
        "predicted_topic_lane": lane,
        "kol_reaction_count": tier1_count,
        "emotional_intensity": float(emo),
        "debate_potential": float(debate),
        "topic_summary": summary[:500],
        "reasoning": str(payload.get("reasoning") or "")[:500],
    }


def _clamp_number(value: Any, lo: float, hi: float, *, default: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n:  # NaN
        return default
    return max(lo, min(hi, n))
