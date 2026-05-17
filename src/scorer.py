"""Draft scorer — five dimensions, total 0-100 (each dim 0-10, weight 20%).

Dimensions (UNIFIED_SPEC §7.2):
  1. info_density  — 信息密度
  2. stance        — 立场强度
  3. counter       — 反共识度
  4. hook          — 钩子强度
  5. compliance    — 合规安全度 (guardrails rejected → 0)

LLM scores dims 1-4 in a single JSON call. Compliance is set deterministically
by the caller (writer.py) based on the GuardrailReport. Pass threshold defaults
to 60 and is read from config/topic_blend.yaml#score_pass_threshold if present.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.guardrails import GuardrailReport, Severity
from src.llm import LLMError, call_llm

__all__ = ["ScoreResult", "score", "score_compliance", "pass_threshold"]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TOPIC_BLEND_PATH = _PROJECT_ROOT / "config" / "topic_blend.yaml"

DIMENSIONS: tuple[str, ...] = ("info_density", "stance", "counter", "hook", "compliance")
_DIM_WEIGHT = 2  # each 0-10 dim × 2 == 0-20; sum of 5 dims = 0-100.

_SCORE_PROMPT = """你是中文金融推文评分员。

按 4 个维度给草稿打分，每个维度 0-10 整数。不要解释，只输出 JSON。

维度定义:
- info_density: 0=空话堆砌 / 10=每句一条事实
- stance: 0=骑墙模糊 / 10=立场清晰可被反驳
- counter: 0=人云亦云 / 10=独到反共识角度
- hook: 0=开头平淡 / 10=首句抓人停下

输出严格 JSON:
{
  "info_density": <0-10>,
  "stance": <0-10>,
  "counter": <0-10>,
  "hook": <0-10>
}

草稿:
"""


@dataclass
class ScoreResult:
    info_density: int
    stance: int
    counter: int
    hook: int
    compliance: int
    total: int
    passed: bool
    raw: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "info_density": self.info_density,
            "stance": self.stance,
            "counter": self.counter,
            "hook": self.hook,
            "compliance": self.compliance,
            "total": self.total,
            "passed": self.passed,
        }


def _clamp(value: object, lo: int = 0, hi: int = 10) -> int:
    try:
        return max(lo, min(hi, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return lo


def score_compliance(report: GuardrailReport | None) -> int:
    """Map a GuardrailReport into a 0-10 compliance score.

    None or passed clean              → 10
    Single B_warn (passed=True)       → 8
    Rejected (A_kill)                 → 0
    """
    if report is None or (report.passed and report.severity is None):
        return 10
    if report.passed and report.severity == Severity.B_WARN:
        return 8
    return 0


def pass_threshold() -> int:
    """Read score_pass_threshold from topic_blend.yaml. Default 60."""
    if not _TOPIC_BLEND_PATH.exists():
        return 60
    try:
        data = yaml.safe_load(_TOPIC_BLEND_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return 60
    return int(data.get("score_pass_threshold", 60))


def _llm_score(draft: str) -> dict[str, int]:
    """Call LLM in JSON mode; clamp dims 1-4."""
    raw = call_llm(_SCORE_PROMPT + draft, response_format="json", max_retries=1)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"scorer parse failed: {exc}") from exc
    return {
        "info_density": _clamp(parsed.get("info_density")),
        "stance": _clamp(parsed.get("stance")),
        "counter": _clamp(parsed.get("counter")),
        "hook": _clamp(parsed.get("hook")),
    }


def score(
    draft: str,
    guardrail_report: GuardrailReport | None = None,
    threshold: int | None = None,
) -> ScoreResult:
    """Score a draft. Returns ScoreResult with total 0-100 and pass flag.

    If LLM fails, dims 1-4 fall back to 0 (compliance still honors guardrails).
    """
    threshold_value = threshold if threshold is not None else pass_threshold()

    try:
        dims = _llm_score(draft)
    except LLMError:
        dims = {"info_density": 0, "stance": 0, "counter": 0, "hook": 0}

    compliance_score = score_compliance(guardrail_report)
    total = (
        dims["info_density"] + dims["stance"] + dims["counter"]
        + dims["hook"] + compliance_score
    ) * _DIM_WEIGHT

    return ScoreResult(
        info_density=dims["info_density"],
        stance=dims["stance"],
        counter=dims["counter"],
        hook=dims["hook"],
        compliance=compliance_score,
        total=total,
        passed=total >= threshold_value,
        raw={**dims, "compliance": compliance_score},
    )
