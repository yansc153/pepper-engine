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
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
    "short":   "≤ 280 字（X 短推）",
    "medium":  "500-1000 字（X 长推文）",
    "long":    "1000-1500 字（X Article 短版）",
    "article": "1500-2000 字（X Article 完整版）",
}


def _length_band_for_source(content_length: int) -> str:
    """Map source article length → target rewrite length tier.

    Per user spec 2026-05-18:
      source >3000 → article (1500-2000 字)
      source 2000-3000 → long (1000-1500 字)
      source 1500-2000 → medium (500-1000 字)
      source <1500 → short (≤ 280 字)
    """
    if content_length > 3000:
        return "article"
    if content_length > 2000:
        return "long"
    if content_length > 1500:
        return "medium"
    return "short"

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


def _pick_source_image_url(topic: dict[str, Any]) -> str | None:
    """Look up the first source observation that has an image_url.

    The topic was clustered from N observations; we pick any one of them as
    the visual material to attach to the draft (later downloaded to
    tmp_images/<draft_id>.jpg by _download_source_image).
    """
    obs_ids = _source_obs_ids(topic)
    if not obs_ids:
        return None
    from src.database import get_conn
    conn = get_conn()
    try:
        placeholders = ",".join(["?"] * len(obs_ids))
        try:
            row = conn.execute(
                f"SELECT image_url FROM reaction_observations "
                f"WHERE id IN ({placeholders}) AND image_url IS NOT NULL "
                f"ORDER BY viral_score DESC LIMIT 1",
                obs_ids,
            ).fetchone()
        except Exception as exc:  # noqa: BLE001 — missing column (old DB) is non-fatal
            logger.debug("image lookup failed (column missing?): %s", exc)
            return None
    finally:
        conn.close()
    return row["image_url"] if row and row["image_url"] else None


def _download_source_image(image_url: str, draft_id: int) -> str | None:
    """Download source image to /app/tmp_images/draft_<id>.jpg. Return local path."""
    import httpx
    from pathlib import Path

    tmp_dir = Path("/app/tmp_images")
    if not tmp_dir.exists():
        tmp_dir = Path(__file__).resolve().parent.parent / "tmp_images"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / f"draft_{draft_id}.jpg"

    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get(image_url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        return str(dest)
    except Exception as exc:  # noqa: BLE001 — image failure must not block draft
        logger.warning("image download failed for draft=%s url=%s: %s",
                       draft_id, image_url, exc)
        return None


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

def _load_source_bodies(topic: dict[str, Any]) -> tuple[str, int]:
    """Pull the actual source observation bodies + max content length.

    The writer needs raw source material to rewrite WITHOUT fabrication;
    the topic_summary alone is too compressed to anchor a 1500+ char rewrite.
    Returns (joined_bodies, max_single_body_length).
    """
    obs_ids = _source_obs_ids(topic)
    if not obs_ids:
        return "", 0
    from src.database import get_conn
    conn = get_conn()
    try:
        placeholders = ",".join(["?"] * len(obs_ids))
        rows = conn.execute(
            f"SELECT content, content_length FROM reaction_observations "
            f"WHERE id IN ({placeholders}) ORDER BY content_length DESC",
            obs_ids,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("source body lookup failed: %s", exc)
        return "", 0
    finally:
        conn.close()
    if not rows:
        return "", 0
    max_len = max(int(r["content_length"] or len(r["content"] or "")) for r in rows)
    bodies = "\n\n---\n\n".join(str(r["content"] or "")[:4000] for r in rows[:3])
    return bodies, max_len


def build_fact_spine(topic_candidate: dict[str, Any]) -> dict[str, Any]:
    """Turn a topic_candidate row into a hard-fact skeleton.

    Cheap deterministic distill; no LLM call (the LLM step happens at draft time).
    """
    summary = (topic_candidate.get("topic_summary") or "").strip()
    facts = [line.strip() for line in summary.split("\n") if line.strip()][:6]
    source_bodies, max_source_len = _load_source_bodies(topic_candidate)
    return {
        "fact_spine": facts,
        "most_telling_fact": facts[0] if facts else "",
        "topic_lane": topic_candidate.get("predicted_topic_lane", ""),
        "virality_score": topic_candidate.get("virality_score", 0.0),
        "source_bodies": source_bodies,
        "max_source_length": max_source_len,
    }


def retrieve_techniques(ctx: dict[str, Any], k: int = 5) -> list[dict[str, Any]]:
    """Pull Top-K technique entries from the pattern miner.

    Builds a `RetrievalContext` dataclass from the loose dict the writer
    pipeline carries. Stub-safe: if miner missing or retrieve raises, return [].
    """
    try:
        import importlib
        miner = importlib.import_module("src.miner")
    except ImportError:
        return []
    fn = getattr(miner, "retrieve", None)
    if fn is None:
        return []
    # Build the typed context; if the dataclass module isn't importable
    # (e.g. test monkeypatches src.miner with a bare module), fall through
    # to the dict and let the miner stub deal with it.
    request: Any
    try:
        from src.miner.types import RetrievalContext
        request = RetrievalContext(
            topic_lane=str(ctx.get("topic_lane") or ""),
            post_hour_utc=int(ctx.get("post_hour_utc", datetime.now(timezone.utc).hour)),
            persona=str(ctx.get("persona") or ""),
            fact_spine_keywords=list(ctx.get("fact_spine_keywords") or []),
            avoid_recent_pattern_ids=list(ctx.get("avoid_recent_pattern_ids") or []),
            content_mode=ctx.get("content_mode"),
        )
    except (ImportError, Exception):  # noqa: BLE001
        request = ctx
    try:
        result = fn(request, k=k)
    except Exception as exc:  # noqa: BLE001 — miner failure must not kill writer
        logger.warning("miner.retrieve failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for entry in result or []:
        if isinstance(entry, dict):
            out.append(entry)
            continue
        out.append({
            "id": getattr(entry, "id", None),
            "hook_pattern": getattr(entry, "hook_pattern", ""),
            "hook": getattr(entry, "hook_pattern", ""),
            "example": getattr(entry, "hook_example", ""),
            "source_excerpt": getattr(entry, "hook_example", ""),
        })
    return out


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
        "source_bodies": fact_spine.get("source_bodies", ""),
        "max_source_length": fact_spine.get("max_source_length", 0),
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

    # OVERRIDE optimal_length based on source body length (user spec 2026-05-18).
    # Topic-scorer's LLM prediction is unreliable; deterministic mapping wins.
    src_len = int(angle_card.get("max_source_length") or 0)
    if src_len > 0:
        optimal_length = _length_band_for_source(src_len)
    length_hint = _LENGTH_BANDS.get(optimal_length, _LENGTH_BANDS["short"])

    hooks_block = "\n".join(f"- {h}" for h in angle_card["hooks"]) or "（无）"
    examples_block = "\n".join(f"- {e}" for e in angle_card["examples"]) or "（无）"
    facts_block = "\n".join(f"- {f}" for f in angle_card["fact_spine"]) or "（无）"
    source_bodies = (angle_card.get("source_bodies") or "").strip()
    source_block = source_bodies[:8000] if source_bodies else "（无源文，禁止编造任何具体数据/事件）"

    return f"""你是中文金融账号写手 persona={persona} lane={topic_lane}。

## ⚠️ 最高优先级硬规则（违反 = 自动重写，扣分）

1. **禁止编造**：所有数字、公司名、人名、股票代码、时间、政策内容必须来自下面"原文"部分。原文没有的绝对不能写。
2. **禁止任何"我"的具体交易动作**：
   - ❌ 不能写：「我上周加了仓」「我模拟盘加到两成」「我真仓只敢给 5%」「我减持」「我止盈」「我建仓」「我账户里」「我手指悬在卖出键上」「我没按下去」「我浮盈垫高 5 个点」「我直接降回半仓」「我数了数」「我先看 X 谁先破位」
   - ✅ 可以写：「市场怎么看」「数据告诉我们」「短期判断」「关键观察位是 X」「这个信号意味着 Y」「机构资金流向显示 Z」
   - 角色定位：你是市场观察者 / 复盘者 / 分析师，不是交易者。可以分析事件、判断方向、给观察位，**绝对不能编造自己做过的具体交易**。
3. **禁止臆造生活比喻**：比如「夜班食堂的炒饭」「油一冷就坨」这种和原文无关的具体生活意象，不能凭空加。比喻只能用原文里出现过的。
4. **目标字数是硬要求**：少于下限或多于上限都算不合格 → 必须按目标长度写完。

## 原文（仿写源 — 你的所有事实/数字/事件都必须来自这里）
{source_block}

## 模板（{content_mode}）
{template}

## 声音
{voice_profile}

## 排版规则
{voice_rules}

## 事实骨架（从原文蒸馏出的关键点）
{facts_block}

## 学到的钩子（参考结构，不抄原句）
{hooks_block}

## 历史成功范例（仅参考节奏）
{examples_block}

## 目标长度
{length_hint}

按目标长度认真写，不要硬截断。**字数要求是硬要求**：少于下限或多于上限都算不合格。

## ⚠️ 写之前再次检查

- 你写的每个数字/公司名/事件都能在"原文"里找到吗？找不到的删掉。
- 你的句子里有出现「我加仓 / 我减仓 / 我买入 / 我卖出 / 我止盈 / 我止损 / 我账户 / 我仓位 / 我模拟盘 / 我真仓 / 我点了卖出 / 我数了 / 我手指 / 我没按 / 我浮盈 / 我打算」吗？有 → 全部删掉，改成第三人称分析口吻。
- 字数到位了吗（{length_hint}）？没到 → 接着写。
- 第一人称「我」最多出现 0-1 次，且只能用于「我观察到 / 我判断 / 我倾向」这类纯分析语言，不能涉及具体交易动作。

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


# Regex catching first-person trade fabrication. LLM keeps inventing personal
# trades despite hard prompt rules, so we scan the output and force rewrite.
_FAB_TRADE_RE = re.compile(
    r"我[^\n。，,.\s]{0,8}("
    r"加仓|减仓|建仓|清仓|割肉|止盈|止损|"
    r"买入|卖出|减持|加|减|清|割|止|"
    r"模拟盘|真仓|账户|仓位|浮盈|浮亏|持仓|"
    r"按.?[卖买]|手指悬|没按|点了|敲了|挂了|"
    r"数了|盯了|看了几眼|刷了几次|"
    r"先看|要不要|打算|准备"
    r")"
)


def detect_fabricated_trades(content: str) -> list[str]:
    """Return list of first-person trade phrases found (empty = clean)."""
    return [m.group(0) for m in _FAB_TRADE_RE.finditer(content)]


def audit_for_template(content: str) -> dict[str, Any]:
    """A-class audit: regex pass (fast, deterministic) + LLM pass (slow, nuanced).

    Regex catches the persistent first-person trade fabrication that the LLM
    keeps producing despite prompt rules. LLM catches everything else.
    """
    fab = detect_fabricated_trades(content)
    if fab:
        return {
            "verdict": "needs_rewrite",
            "why_it_reads_ai": [
                f"编造了第一人称交易动作：{', '.join(fab[:5])}"
            ],
            "rewrite_focus": (
                "删除所有「我加仓/减仓/建仓/止盈/止损/账户/仓位/模拟盘/真仓」"
                "等任何具体交易动作的语言，改成第三人称分析口吻。"
            ),
        }

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
    # Last-line fab gate: if the rewrite loop couldn't shake the first-person
    # trade fabrication, DROP the draft. Better to ship 0 than ship a lie.
    fab_hits = detect_fabricated_trades(draft["content"])
    if fab_hits:
        logger.warning(
            "DROPPING draft for topic %s — fab phrases survived rewrite loop: %s",
            topic.get("id"), fab_hits,
        )
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
            score_total=None,
            error=f"fabrication survived rewrite: {fab_hits[:3]}",
            score_breakdown=None,
        )

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

    # HARD IMAGE REQUIREMENT (user spec 2026-05-18): drop draft if no source
    # observation has an image — Twitter engagement drops sharply for text-only
    # posts, and user said "没有配图一律不看".
    src_image_url = _pick_source_image_url(topic)
    if not src_image_url:
        logger.info("topic %s has no source image — dropping draft", topic.get("id"))
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
            error="no source image available",
            score_breakdown=score_result.to_dict(),
        )

    # Persist first to get draft_id, then download image (need id for filename).
    try:
        draft_id = _persist_draft(draft["content"], topic, pattern_ids, image_path=None)
    except Exception as exc:  # noqa: BLE001 — persistence failure surfaces as error
        return _fail_result(topic, f"persist failed: {exc}", content=draft["content"])

    image_path: str | None = _download_source_image(src_image_url, draft_id)
    if image_path:
        from src.database import get_conn
        conn = get_conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE drafts SET image_path = ? WHERE id = ?",
                    (image_path, draft_id),
                )
        finally:
            conn.close()

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
        image_path=image_path,
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
