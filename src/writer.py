"""Writer pipeline — fact spine → angle card → draft → audit → guardrails → score.

Entry: `write_draft(topic_candidate, persona=None) -> DraftResult`.

Pipeline (UNIFIED_SPEC §7.1, §16.5, §16.6):
1. build_fact_spine(topic)
2. retrieve_techniques(ctx) via miner.retrieve(k=5)  [stub-safe]
3. build_angle_card(fact_spine, retrieved_techniques)
4. draft(angle_card, persona, content_mode)         [LLM]
5. audit_for_template(draft)                        [A-class slop rewrite loop]
6. guardrails.check(draft, persona, stance)         [MAX_REWRITE_ATTEMPTS=3]
7. scorer.score(draft)                              [5-dim, threshold from config]
8. pass=score_total>=threshold → INSERT drafts table, return DraftResult.

Variable-length: writer respects topic_candidate.predicted_length and tells the
LLM the target band. 280 char hard cap removed.

Content modes: insight / meme / emotional → loads matching template file.

Exhausting guardrail retries raises GuardrailsExhausted (caller catches and
writes circuit_breaker).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.guardrails import (
    GuardrailReport,
    GuardrailsExhausted,
    MAX_REWRITE_ATTEMPTS,
    check as guardrails_check,
    load_lexicons,
    trip_circuit_breaker,
)
from src.llm import LLMError, call_llm
from src.scorer import ScoreResult, score as scorer_score, score_compliance

logger = logging.getLogger(__name__)

__all__ = [
    "DraftResult",
    "write_draft",
    "build_fact_spine",
    "retrieve_techniques",
    "build_angle_card",
]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _PROJECT_ROOT / "templates"
_VOICE_DIR = _PROJECT_ROOT / "voice"

_TEMPLATE_FILES = {
    "insight": _TEMPLATES_DIR / "template_finance_insight.md",
    "meme": _TEMPLATES_DIR / "template_finance_meme.md",
    "emotional": _TEMPLATES_DIR / "template_finance_emotional.md",
}

_LENGTH_BANDS = {
    "short": "≤ 280 字",
    "medium": "281-1000 字",
    "long": "1001-2500 字",
    "article": "> 2500 字（X Article）",
}

_AUDIT_PROMPT = """你是中文社交文案审稿人。

检查这条推文是否太像 AI 总结腔 / KOL 模板腔 / 为发而发。
只判断 pass 或 needs_rewrite，并指出最该改的 1-3 处。

输出 JSON:
{
  "verdict": "pass" | "needs_rewrite",
  "why_it_reads_ai": ["..."],
  "rewrite_focus": "..."
}

待审推文:
"""


# ────────────────────────────────────────────────────────────
# Data class
# ────────────────────────────────────────────────────────────

@dataclass
class DraftResult:
    success: bool
    draft_id: int | None
    content: str | None
    content_length: int
    content_mode: str
    optimal_length: str
    topic_lane: str
    persona: str
    pattern_ids: list[int]
    image_path: str | None
    score_total: int | None
    error: str | None
    score_breakdown: dict[str, Any] = field(default_factory=dict)


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _read_text(path: Path, max_chars: int | None = None) -> str:
    if not path.exists():
        return ""
    body = path.read_text(encoding="utf-8")
    if max_chars is not None:
        body = body[:max_chars]
    return body


def _resolve_template(content_mode: str) -> str:
    path = _TEMPLATE_FILES.get(content_mode)
    if path is None:
        raise ValueError(
            f"unknown content_mode '{content_mode}'; expected one of {list(_TEMPLATE_FILES)}"
        )
    return _read_text(path, max_chars=2500)


def _normalize_topic(topic_candidate: dict[str, Any]) -> dict[str, Any]:
    """Fill defaults so writer never crashes on missing keys."""
    return {
        "id": topic_candidate.get("id"),
        "topic_summary": topic_candidate.get("topic_summary", ""),
        "source_observations": topic_candidate.get("source_observations", "[]"),
        "predicted_content_mode": topic_candidate.get("predicted_content_mode") or "insight",
        "predicted_length": topic_candidate.get("predicted_length") or "short",
        "predicted_topic_lane": topic_candidate.get("predicted_topic_lane") or "general_tech_ai",
        "persona": topic_candidate.get("persona") or "finance_neutral",
        "virality_score": topic_candidate.get("virality_score", 0.0),
    }


def _source_obs_ids(topic: dict[str, Any]) -> list[int]:
    raw = topic.get("source_observations") or "[]"
    if isinstance(raw, list):
        return [int(x) for x in raw]
    try:
        return [int(x) for x in json.loads(raw)]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


# ────────────────────────────────────────────────────────────
# Pipeline steps
# ────────────────────────────────────────────────────────────

def build_fact_spine(topic_candidate: dict[str, Any]) -> dict[str, Any]:
    """Turn a topic_candidate row into a hard-fact skeleton.

    Cheap deterministic distill; no LLM call (the LLM step happens at draft time).
    """
    summary = (topic_candidate.get("topic_summary") or "").strip()
    facts = [line.strip() for line in summary.split("\n") if line.strip()][:6]
    return {
        "fact_spine": facts,
        "most_telling_fact": facts[0] if facts else "",
        "topic_lane": topic_candidate.get("predicted_topic_lane", ""),
        "virality_score": topic_candidate.get("virality_score", 0.0),
    }


def retrieve_techniques(ctx: dict[str, Any], k: int = 5) -> list[dict[str, Any]]:
    """Pull Top-K technique entries from the pattern miner.

    Stub-safe: if `src.miner` doesn't exist or `retrieve` raises, return [].
    """
    try:
        import importlib
        miner = importlib.import_module("src.miner")
    except ImportError:
        return []
    fn = getattr(miner, "retrieve", None)
    if fn is None:
        return []
    try:
        result = fn(ctx, k=k)
    except Exception as exc:  # noqa: BLE001 — miner failure must not kill writer
        logger.warning("miner.retrieve failed: %s", exc)
        return []
    return list(result or [])


def build_angle_card(
    fact_spine: dict[str, Any],
    retrieved_techniques: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compose the angle card injected into the writer prompt."""
    hooks = []
    examples = []
    pattern_ids: list[int] = []
    for entry in retrieved_techniques[:5]:
        hook = entry.get("hook_pattern") or entry.get("hook") or ""
        example = entry.get("example") or entry.get("source_excerpt") or ""
        eid = entry.get("id")
        if hook:
            hooks.append(hook)
        if example:
            examples.append(example)
        if isinstance(eid, int):
            pattern_ids.append(eid)
    return {
        "hooks": hooks,
        "examples": examples,
        "pattern_ids": pattern_ids,
        "most_telling_fact": fact_spine.get("most_telling_fact", ""),
        "fact_spine": fact_spine.get("fact_spine", []),
    }


def _build_writer_prompt(
    angle_card: dict[str, Any],
    persona: str,
    content_mode: str,
    optimal_length: str,
    topic_lane: str,
) -> str:
    template = _resolve_template(content_mode)
    voice_profile = _read_text(_VOICE_DIR / "voice_profile.md", max_chars=2000)
    voice_rules = _read_text(_VOICE_DIR / "voice_rules.md", max_chars=1500)
    length_hint = _LENGTH_BANDS.get(optimal_length, _LENGTH_BANDS["short"])
    hooks_block = "\n".join(f"- {h}" for h in angle_card["hooks"]) or "（无）"
    examples_block = "\n".join(f"- {e}" for e in angle_card["examples"]) or "（无）"
    facts_block = "\n".join(f"- {f}" for f in angle_card["fact_spine"]) or "（无）"

    return f"""你是中文金融账号写手 persona={persona} lane={topic_lane}。

## 模板（{content_mode}）
{template}

## 声音
{voice_profile}

## 排版规则
{voice_rules}

## 事实骨架
{facts_block}

## 学到的钩子（参考结构，不抄原句）
{hooks_block}

## 历史成功范例（仅参考节奏）
{examples_block}

## 目标长度
{length_hint}（按此长度写，不要硬截断）

## 输出 JSON
{{
  "content": "<推文正文>",
  "image_prompt": "<配图描述，若无则空字符串>",
  "stance_strength": <1-5 整数，本帖的立场强度>
}}
"""


def _draft_once(prompt: str) -> dict[str, Any]:
    """Single LLM call. Returns parsed dict with content/image/stance."""
    raw = call_llm(prompt, response_format="json", max_retries=1)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"writer parse failed: {exc}") from exc
    return {
        "content": str(parsed.get("content", "")).strip(),
        "image_prompt": str(parsed.get("image_prompt", "")).strip() or None,
        "stance_strength": _coerce_stance(parsed.get("stance_strength", 2)),
    }


def _coerce_stance(value: object) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 2
    return max(1, min(5, n))


def audit_for_template(content: str) -> dict[str, Any]:
    """A-class anti-template audit. Returns verdict dict; on LLM error, pass."""
    try:
        raw = call_llm(_AUDIT_PROMPT + content, response_format="json", max_retries=1)
        parsed = json.loads(raw)
    except (LLMError, json.JSONDecodeError) as exc:
        logger.warning("audit failed, defaulting to pass: %s", exc)
        return {"verdict": "pass", "why_it_reads_ai": [], "rewrite_focus": ""}
    return {
        "verdict": parsed.get("verdict", "pass"),
        "why_it_reads_ai": parsed.get("why_it_reads_ai", []) or [],
        "rewrite_focus": parsed.get("rewrite_focus", "") or "",
    }


# ────────────────────────────────────────────────────────────
# Persistence
# ────────────────────────────────────────────────────────────

def _persist_draft(
    content: str,
    topic: dict[str, Any],
    pattern_ids: list[int],
    image_path: str | None,
) -> int:
    """Insert a candidate draft row. Returns draft_id."""
    from src.content_match import content_hash
    from src.database import get_conn, with_retry

    c_hash = content_hash(content)

    def _write() -> int:
        conn = get_conn()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO drafts (content, content_hash, content_length, "
                    "content_mode, optimal_length, topic_lane, persona, "
                    "pattern_ids, source_observation_ids, image_path, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate')",
                    (
                        content,
                        c_hash,
                        len(content),
                        topic["predicted_content_mode"],
                        topic["predicted_length"],
                        topic["predicted_topic_lane"],
                        topic["persona"],
                        json.dumps(pattern_ids),
                        json.dumps(_source_obs_ids(topic)),
                        image_path,
                    ),
                )
                return int(cur.lastrowid)
        finally:
            conn.close()

    return with_retry(_write)


# ────────────────────────────────────────────────────────────
# Public entry
# ────────────────────────────────────────────────────────────

async def write_draft(
    topic_candidate: dict[str, Any],
    persona: str | None = None,
) -> DraftResult:
    """Run the full writer pipeline on a topic_candidate row.

    `async` to match the orchestrator contract; the body is sync because
    call_llm is sync subprocess/httpx. We don't await anything inside.
    """
    topic = _normalize_topic(topic_candidate)
    if persona is not None:
        topic["persona"] = persona

    lexicons = load_lexicons()
    fact_spine = build_fact_spine(topic)
    techniques = retrieve_techniques(
        {
            "topic_lane": topic["predicted_topic_lane"],
            "content_mode": topic["predicted_content_mode"],
            "persona": topic["persona"],
        },
        k=5,
    )
    angle_card = build_angle_card(fact_spine, techniques)

    base_prompt = _build_writer_prompt(
        angle_card=angle_card,
        persona=topic["persona"],
        content_mode=topic["predicted_content_mode"],
        optimal_length=topic["predicted_length"],
        topic_lane=topic["predicted_topic_lane"],
    )

    last_report: GuardrailReport | None = None
    last_draft: dict[str, Any] | None = None
    prompt = base_prompt

    for attempt in range(MAX_REWRITE_ATTEMPTS):
        try:
            draft = _draft_once(prompt)
        except LLMError as exc:
            logger.warning("draft attempt %d LLM error: %s", attempt + 1, exc)
            return _fail_result(topic, f"LLM error: {exc}")

        if not draft["content"]:
            prompt = base_prompt + "\n\n上一次输出为空，请重写。"
            continue

        last_draft = draft

        audit = audit_for_template(draft["content"])
        if audit["verdict"] == "needs_rewrite" and attempt < MAX_REWRITE_ATTEMPTS - 1:
            prompt = (
                base_prompt
                + f"\n\n上一版被判定为模板腔。问题:\n"
                + "\n".join(f"- {r}" for r in audit["why_it_reads_ai"])
                + f"\n重点改: {audit['rewrite_focus']}\n请重写。"
            )
            continue

        report = guardrails_check(
            draft["content"],
            persona=topic["persona"],
            stance_strength=draft["stance_strength"],
            lexicons=lexicons,
        )
        last_report = report

        if report.passed:
            return _finalize(topic, draft, angle_card["pattern_ids"], report)

        if attempt >= MAX_REWRITE_ATTEMPTS - 1:
            break

        prompt = (
            base_prompt
            + "\n\n上一版触发合规拦截:\n"
            + "\n".join(f"- {r}" for r in report.reasons)
            + f"\n命中: {report.matched}\n请避开上述词汇/股票代码后重写。"
        )

    # Exhausted: trip circuit breaker and raise.
    reason = (
        f"guardrails exhausted after {MAX_REWRITE_ATTEMPTS} attempts; "
        f"last_matched={last_report.matched if last_report else []}"
    )
    try:
        trip_circuit_breaker("writer.guardrails", reason, reset_after_seconds=3600)
    except Exception as exc:  # noqa: BLE001
        logger.error("circuit breaker write failed: %s", exc)
    raise GuardrailsExhausted(reason)


def _finalize(
    topic: dict[str, Any],
    draft: dict[str, Any],
    pattern_ids: list[int],
    report: GuardrailReport,
) -> DraftResult:
    """Score, persist if pass, and build the DraftResult."""
    score_result: ScoreResult = scorer_score(draft["content"], guardrail_report=report)

    if not score_result.passed:
        return DraftResult(
            success=False,
            draft_id=None,
            content=draft["content"],
            content_length=len(draft["content"]),
            content_mode=topic["predicted_content_mode"],
            optimal_length=topic["predicted_length"],
            topic_lane=topic["predicted_topic_lane"],
            persona=topic["persona"],
            pattern_ids=pattern_ids,
            image_path=None,
            score_total=score_result.total,
            error=f"score {score_result.total} below threshold",
            score_breakdown=score_result.to_dict(),
        )

    try:
        draft_id = _persist_draft(draft["content"], topic, pattern_ids, image_path=None)
    except Exception as exc:  # noqa: BLE001 — persistence failure surfaces as error
        return _fail_result(topic, f"persist failed: {exc}", content=draft["content"])

    return DraftResult(
        success=True,
        draft_id=draft_id,
        content=draft["content"],
        content_length=len(draft["content"]),
        content_mode=topic["predicted_content_mode"],
        optimal_length=topic["predicted_length"],
        topic_lane=topic["predicted_topic_lane"],
        persona=topic["persona"],
        pattern_ids=pattern_ids,
        image_path=None,
        score_total=score_result.total,
        error=None,
        score_breakdown=score_result.to_dict(),
    )


def _fail_result(
    topic: dict[str, Any],
    error: str,
    content: str | None = None,
) -> DraftResult:
    return DraftResult(
        success=False,
        draft_id=None,
        content=content,
        content_length=len(content) if content else 0,
        content_mode=topic["predicted_content_mode"],
        optimal_length=topic["predicted_length"],
        topic_lane=topic["predicted_topic_lane"],
        persona=topic["persona"],
        pattern_ids=[],
        image_path=None,
        score_total=None,
        error=error,
    )


# Convenience: expose dataclass-asdict for tests / observability.
def result_to_dict(result: DraftResult) -> dict[str, Any]:
    return asdict(result)
