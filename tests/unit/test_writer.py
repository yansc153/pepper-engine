"""Unit tests for src/writer.py — fact spine → angle card → draft → audit → guardrails → score."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from src import scorer as scorer_mod
from src import writer as writer_mod
from src.guardrails import GuardrailsExhausted
from src.llm import LLMError
from src.writer import (
    DraftResult,
    build_angle_card,
    build_fact_spine,
    retrieve_techniques,
    write_draft,
)


# ─────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────

@pytest.fixture()
def topic() -> dict[str, Any]:
    return {
        "id": 42,
        "topic_summary": "央行降准 0.25 个百分点\n释放长期资金约 5000 亿\n房地产板块尾盘异动",
        "source_observations": json.dumps([101, 102]),
        "predicted_content_mode": "insight",
        "predicted_length": "short",
        "predicted_topic_lane": "intraday",
        "persona": "finance_neutral",
        "virality_score": 0.7,
    }


def _writer_llm(content: str = "央行这次降准节奏比预期快", stance: int = 2) -> str:
    return json.dumps({
        "content": content,
        "image_prompt": "央行大楼夜景",
        "stance_strength": stance,
    })


def _audit_pass() -> str:
    return json.dumps({"verdict": "pass", "why_it_reads_ai": [], "rewrite_focus": ""})


def _audit_rewrite() -> str:
    return json.dumps({"verdict": "needs_rewrite",
                       "why_it_reads_ai": ["开头模板"],
                       "rewrite_focus": "首句"})


def _score_pass() -> str:
    return json.dumps({"info_density": 8, "stance": 7, "counter": 7, "hook": 8})


def _score_fail() -> str:
    return json.dumps({"info_density": 1, "stance": 1, "counter": 1, "hook": 1})


@pytest.fixture()
def patch_llm(monkeypatch: pytest.MonkeyPatch):
    """Sequence-based LLM patcher. Returns a configurable list."""
    queue: list[str] = []

    def fake_writer_llm(prompt: str, **kw: object) -> str:
        if not queue:
            raise AssertionError(f"unexpected extra LLM call: {prompt[:80]}")
        return queue.pop(0)

    monkeypatch.setattr(writer_mod, "call_llm", fake_writer_llm)
    monkeypatch.setattr(scorer_mod, "call_llm", fake_writer_llm)
    return queue


@pytest.fixture()
def fake_db(monkeypatch: pytest.MonkeyPatch):
    """Fake _persist_draft to avoid touching SQLite."""
    inserted: list[dict[str, Any]] = []

    def fake_persist(content, topic, pattern_ids, image_path):
        rec = {
            "content": content,
            "topic": dict(topic),
            "pattern_ids": list(pattern_ids),
            "image_path": image_path,
        }
        inserted.append(rec)
        return 9001 + len(inserted)

    monkeypatch.setattr(writer_mod, "_persist_draft", fake_persist)
    return inserted


@pytest.fixture()
def no_miner(monkeypatch: pytest.MonkeyPatch):
    """Force miner.retrieve to return []."""
    # If src.miner is importable as a package, stub its retrieve.
    fake = types.ModuleType("src.miner")
    fake.retrieve = lambda ctx, k=5: []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.miner", fake)
    return fake


# ─────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────

def test_build_fact_spine_from_summary(topic: dict[str, Any]) -> None:
    spine = build_fact_spine(topic)
    assert len(spine["fact_spine"]) == 3
    assert spine["most_telling_fact"] == "央行降准 0.25 个百分点"
    assert spine["topic_lane"] == "intraday"


def test_build_fact_spine_empty_summary() -> None:
    spine = build_fact_spine({"topic_summary": ""})
    assert spine["fact_spine"] == []
    assert spine["most_telling_fact"] == ""


def test_retrieve_techniques_no_miner(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure no src.miner module
    monkeypatch.setitem(sys.modules, "src.miner", types.ModuleType("src.miner"))
    out = retrieve_techniques({"topic_lane": "x"}, k=5)
    assert out == []


def test_retrieve_techniques_swallows_miner_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("src.miner")
    def boom(ctx, k=5):
        raise RuntimeError("miner down")
    fake.retrieve = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.miner", fake)
    assert retrieve_techniques({}, k=3) == []


def test_retrieve_techniques_returns_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("src.miner")
    fake.retrieve = lambda ctx, k=5: [  # type: ignore[attr-defined]
        {"id": 1, "hook_pattern": "数字反差", "example": "去年这周也是这样"},
    ]
    monkeypatch.setitem(sys.modules, "src.miner", fake)
    out = retrieve_techniques({"topic_lane": "intraday"}, k=5)
    assert len(out) == 1 and out[0]["hook_pattern"] == "数字反差"


def test_build_angle_card_extracts_pattern_ids() -> None:
    spine = {"most_telling_fact": "A", "fact_spine": ["A", "B"]}
    entries = [
        {"id": 11, "hook_pattern": "h1", "example": "e1"},
        {"id": 12, "hook_pattern": "h2", "example": "e2"},
        {"hook_pattern": "h3", "example": ""},  # no id → not added to ids
    ]
    card = build_angle_card(spine, entries)
    assert card["pattern_ids"] == [11, 12]
    assert card["hooks"] == ["h1", "h2", "h3"]
    assert card["examples"] == ["e1", "e2"]


# ─────────────────────────────────────────────────────────
# write_draft happy path + failure modes
# ─────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def test_write_draft_happy_path(
    topic: dict[str, Any], patch_llm: list[str], fake_db: list[dict[str, Any]], no_miner
) -> None:
    patch_llm.extend([_writer_llm(), _audit_pass(), _score_pass()])
    result = _run(write_draft(topic))
    assert isinstance(result, DraftResult)
    assert result.success is True
    assert result.draft_id is not None
    assert result.content_length == len(result.content or "")
    assert result.content_mode == "insight"
    assert result.persona == "finance_neutral"
    assert result.topic_lane == "intraday"
    assert result.error is None
    assert result.score_total is not None and result.score_total >= 60
    assert len(fake_db) == 1


def test_write_draft_persona_override(
    topic: dict[str, Any], patch_llm: list[str], fake_db: list[dict[str, Any]], no_miner
) -> None:
    patch_llm.extend([_writer_llm(), _audit_pass(), _score_pass()])
    result = _run(write_draft(topic, persona="finance_contrarian"))
    assert result.persona == "finance_contrarian"
    assert fake_db[0]["topic"]["persona"] == "finance_contrarian"


def test_write_draft_audit_rewrite_then_pass(
    topic: dict[str, Any], patch_llm: list[str], fake_db: list[dict[str, Any]], no_miner
) -> None:
    # attempt1: draft+audit rewrite; attempt2: draft+audit pass+score pass
    patch_llm.extend([
        _writer_llm("v1 太像模板的开头"), _audit_rewrite(),
        _writer_llm("v2 真实视角的开头"), _audit_pass(), _score_pass(),
    ])
    result = _run(write_draft(topic))
    assert result.success is True
    assert result.content == "v2 真实视角的开头"


def test_write_draft_unknown_content_mode_raises(
    topic: dict[str, Any], patch_llm: list[str], no_miner
) -> None:
    topic["predicted_content_mode"] = "bogus"
    # write_draft constructs the prompt up-front → ValueError before LLM call
    with pytest.raises(ValueError, match="unknown content_mode"):
        _run(write_draft(topic))


def test_write_draft_llm_error_returns_failure(
    topic: dict[str, Any], monkeypatch: pytest.MonkeyPatch, no_miner
) -> None:
    def boom(*a: object, **kw: object) -> str:
        raise LLMError("backend down")
    monkeypatch.setattr(writer_mod, "call_llm", boom)
    result = _run(write_draft(topic))
    assert result.success is False
    assert "LLM error" in (result.error or "")
    assert result.draft_id is None


def test_write_draft_guardrails_exhausted_raises(
    topic: dict[str, Any], patch_llm: list[str], no_miner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every draft trips A_kill via 稳赚不赔. Audit always pass.
    bad = json.dumps({"content": "这只稳赚不赔", "image_prompt": "", "stance_strength": 2})
    for _ in range(writer_mod.MAX_REWRITE_ATTEMPTS):
        patch_llm.extend([bad, _audit_pass()])
    # Stub the circuit breaker so we don't touch DB.
    cb_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(writer_mod, "trip_circuit_breaker",
                        lambda scope, reason, reset_after_seconds=3600:
                        cb_calls.append((scope, reason)))
    with pytest.raises(GuardrailsExhausted):
        _run(write_draft(topic))
    assert cb_calls and cb_calls[0][0] == "writer.guardrails"


def test_write_draft_third_attempt_can_succeed(
    topic: dict[str, Any], patch_llm: list[str], fake_db: list[dict[str, Any]], no_miner,
) -> None:
    """Off-by-one boundary: 3 attempts allowed; the 3rd CAN succeed, not auto-fail."""
    bad = json.dumps({"content": "这只稳赚不赔", "image_prompt": "", "stance_strength": 2})
    good = _writer_llm("一段中性观察 节奏稳")
    # attempts 1+2 trip A_kill, attempt 3 passes guardrails → must reach scorer + persist
    patch_llm.extend([bad, _audit_pass(), bad, _audit_pass(), good, _audit_pass(), _score_pass()])
    result = _run(write_draft(topic))
    assert result.success is True, (
        "3rd attempt passing guardrails must succeed, not raise GuardrailsExhausted"
    )
    assert result.draft_id is not None
    assert len(fake_db) == 1


def test_write_draft_score_below_threshold_marks_fail(
    topic: dict[str, Any], patch_llm: list[str], fake_db: list[dict[str, Any]], no_miner
) -> None:
    # Guardrails will pass (clean content), but scorer returns low dims → total < 60.
    patch_llm.extend([_writer_llm("一段中性观察 节奏稳"), _audit_pass(), _score_fail()])
    result = _run(write_draft(topic))
    assert result.success is False
    assert result.score_total is not None
    assert "below threshold" in (result.error or "")
    assert fake_db == []  # not persisted


def test_write_draft_empty_content_retries(
    topic: dict[str, Any], patch_llm: list[str], fake_db: list[dict[str, Any]], no_miner
) -> None:
    empty = json.dumps({"content": "", "image_prompt": "", "stance_strength": 2})
    patch_llm.extend([
        empty,
        # second attempt produces content
        _writer_llm("第二次产出"), _audit_pass(), _score_pass(),
    ])
    result = _run(write_draft(topic))
    assert result.success is True
    assert result.content == "第二次产出"


def test_dataclass_default_factory() -> None:
    r = DraftResult(success=False, draft_id=None, content=None, content_length=0,
                    content_mode="insight", optimal_length="short", topic_lane="x",
                    persona="p", pattern_ids=[], image_path=None,
                    score_total=None, error="x")
    assert r.score_breakdown == {}
