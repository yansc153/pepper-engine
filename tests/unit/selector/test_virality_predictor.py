"""Tests for selector.virality_predictor."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from selector.virality_predictor import (  # noqa: E402
    PROMPT_PATH,
    VirialityPredictionError,
    predict_virality,
)


_OK_PAYLOAD = {
    "virality_score": 80,
    "predicted_content_mode": "insight",
    "predicted_length": "medium",
    "predicted_topic_lane": "pre_market",
    "kol_reaction_count": 999,  # should be overridden by deterministic recount
    "emotional_intensity": 0.7,
    "debate_potential": 0.8,
    "topic_summary": "纳指夜间大跌",
    "reasoning": "三位 tier-1 KOL 都在讨论",
}


def _obs(handle="a", tier=1, content="纳指 跌 4%"):
    return {
        "id": hash(handle + content) & 0xFFFF,
        "author_handle": handle,
        "author_tier": tier,
        "content": content,
        "likes": 100,
        "replies": 30,
    }


def _caller_returning(payload):
    def _fn(prompt, **kwargs):
        assert kwargs.get("response_format") == "json"
        return json.dumps(payload)
    return _fn


def test_prompt_file_exists_and_has_placeholders():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "{cluster_block}" in text
    assert "{pattern_block}" in text


def test_predict_virality_happy_path_recomputes_kol_count():
    out = predict_virality(
        [_obs("a", 1), _obs("b", 1), _obs("c", 3)],
        llm_caller=_caller_returning(_OK_PAYLOAD),
    )
    assert out["virality_score"] == 80.0
    assert out["predicted_content_mode"] == "insight"
    assert out["kol_reaction_count"] == 2  # only tier-1 counted, ignoring LLM 999
    assert out["topic_summary"] == "纳指夜间大跌"


def test_predict_virality_clamps_numbers():
    payload = dict(_OK_PAYLOAD, virality_score=500, emotional_intensity=2.5, debate_potential=-1)
    out = predict_virality([_obs()], llm_caller=_caller_returning(payload))
    assert out["virality_score"] == 100.0
    assert out["emotional_intensity"] == 1.0
    assert out["debate_potential"] == 0.0


def test_predict_virality_rejects_bad_mode():
    payload = dict(_OK_PAYLOAD, predicted_content_mode="rant")
    with pytest.raises(VirialityPredictionError, match="content_mode"):
        predict_virality([_obs()], llm_caller=_caller_returning(payload))


def test_predict_virality_rejects_bad_lane():
    payload = dict(_OK_PAYLOAD, predicted_topic_lane="moon")
    with pytest.raises(VirialityPredictionError, match="lane"):
        predict_virality([_obs()], llm_caller=_caller_returning(payload))


def test_predict_virality_rejects_empty_cluster():
    with pytest.raises(VirialityPredictionError, match="empty"):
        predict_virality([], llm_caller=_caller_returning(_OK_PAYLOAD))


def test_predict_virality_rejects_non_json():
    def bad(prompt, **kwargs):
        return "not json{"
    with pytest.raises(VirialityPredictionError, match="non-JSON"):
        predict_virality([_obs()], llm_caller=bad)


def test_predict_virality_propagates_llm_error():
    from llm import LLMError
    def boom(prompt, **kwargs):
        raise LLMError("backend down")
    with pytest.raises(VirialityPredictionError, match="llm call failed"):
        predict_virality([_obs()], llm_caller=boom)


def test_predict_virality_requires_topic_summary():
    payload = dict(_OK_PAYLOAD, topic_summary="   ")
    with pytest.raises(VirialityPredictionError, match="topic_summary"):
        predict_virality([_obs()], llm_caller=_caller_returning(payload))


def test_prompt_includes_pattern_block_when_provided():
    seen = {}
    def cap(prompt, **kwargs):
        seen["prompt"] = prompt
        return json.dumps(_OK_PAYLOAD)
    predict_virality(
        [_obs()],
        historical_patterns=[{"hook_pattern": "反差对比", "success_score": 88, "topic_lane": "pre_market"}],
        llm_caller=cap,
    )
    assert "反差对比" in seen["prompt"]
    assert "cold start" not in seen["prompt"]
