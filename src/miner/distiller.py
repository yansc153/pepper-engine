"""Light-weight + bulk distillers (UNIFIED_SPEC §6.1).

`light_distill(observation_id)` runs at the tail of each observer pull;
`full_distill(since)` is a backstop the nightly cron uses to mop up failures.
Validation + persona-leak detection live here so prompt template + Python
checks stay in lock-step.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.database import get_conn, with_retry
from src.llm import LLMError, call_llm
from src.miner.db import upsert_entry

__all__ = [
    "light_distill",
    "full_distill",
    "validate_distillation",
    "DistillError",
    "PROMPT_PATH",
]

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "distill.txt"

_HOOK_PATTERNS = frozenset(
    {
        "反共识开场",
        "数字暴击",
        "场景代入",
        "反问",
        "金句压尾",
        "对比悖论",
        "身份代入",
    }
)
_SYNTAX_SIGS = frozenset(
    {"short_comma_no_period", "long_run_on", "stacked_short", "dialog_style"}
)
_EMOTIONS = frozenset(
    {"FOMO", "嘲讽", "认知优越", "焦虑", "共情", "猎奇", "愤怒"}
)
_IMAGE_STYLES = frozenset(
    {"kline_with_doodle", "screenshot", "meme", "chart", "photo", "none"}
)
_LANES = frozenset(
    {
        "pre_market",
        "intraday",
        "post_market",
        "overnight",
        "general_tech_ai",
        "general_meme_career",
        "other",
    }
)
_PERSONAS = frozenset(
    {"finance_neutral", "finance_contrarian", "finance_macro", "general_observer"}
)
_CONTENT_MODES = frozenset({"insight", "meme", "emotional"})
_OPTIMAL_LENGTHS = frozenset({"short", "medium", "long", "article"})

# Persona-leak detector: @-handles or stock tickers in hook_example -> reject.
_TICKER_RE = re.compile(
    r"(?:@\w+)|(?:\b(?:60|00|30|68)\d{4}\b)|(?:\bHK\d{4}\b)|(?:\b[A-Z]{1,5}\b)"
)


class DistillError(ValueError):
    """Raised when LLM output fails schema or persona-leak checks."""


def validate_distillation(payload: dict[str, Any]) -> dict[str, Any]:
    """Strict whitelist check; returns the normalised dict."""
    required = {
        "hook_pattern",
        "hook_example",
        "syntax_signature",
        "sentence_len_avg",
        "sentence_len_p90",
        "stance_strength",
        "emotion_triggers",
        "image_style",
        "topic_lane",
        "applicable_personas",
        "content_mode",
        "optimal_length",
    }
    missing = required - payload.keys()
    if missing:
        raise DistillError(f"missing fields: {sorted(missing)}")

    if payload["hook_pattern"] not in _HOOK_PATTERNS:
        raise DistillError(f"bad hook_pattern: {payload['hook_pattern']!r}")
    if payload["syntax_signature"] not in _SYNTAX_SIGS:
        raise DistillError(f"bad syntax_signature: {payload['syntax_signature']!r}")
    if payload["image_style"] not in _IMAGE_STYLES:
        raise DistillError(f"bad image_style: {payload['image_style']!r}")
    if payload["topic_lane"] not in _LANES:
        raise DistillError(f"bad topic_lane: {payload['topic_lane']!r}")
    if payload["content_mode"] not in _CONTENT_MODES:
        raise DistillError(f"bad content_mode: {payload['content_mode']!r}")
    if payload["optimal_length"] not in _OPTIMAL_LENGTHS:
        raise DistillError(f"bad optimal_length: {payload['optimal_length']!r}")

    stance = int(payload["stance_strength"])
    if stance < 0 or stance > 5:
        raise DistillError(f"stance_strength out of range: {stance}")
    payload["stance_strength"] = stance

    emotions = payload.get("emotion_triggers") or []
    if not isinstance(emotions, list) or len(emotions) > 3:
        raise DistillError(f"emotion_triggers must be list of <=3: {emotions!r}")
    bad_e = [e for e in emotions if e not in _EMOTIONS]
    if bad_e:
        raise DistillError(f"bad emotion: {bad_e}")

    personas = payload.get("applicable_personas") or []
    if not isinstance(personas, list) or not personas:
        raise DistillError("applicable_personas must be non-empty list")
    bad_p = [p for p in personas if p not in _PERSONAS]
    if bad_p:
        raise DistillError(f"bad persona: {bad_p}")

    hook_ex = str(payload["hook_example"])
    if _TICKER_RE.search(hook_ex):
        raise DistillError(f"hook_example leaks identity: {hook_ex!r}")

    payload["sentence_len_avg"] = float(payload["sentence_len_avg"])
    payload["sentence_len_p90"] = float(payload["sentence_len_p90"])
    return payload


def _load_observation(observation_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, content, posted_at, topic_hint "
            "FROM reaction_observations WHERE id = ?",
            (observation_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return dict(row)


def _stamp_distilled(observation_id: int) -> None:
    """Mark obs as processed even on failure — prevents nightly avalanche retries."""

    def _write() -> None:
        conn = get_conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE reaction_observations SET distilled_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (observation_id,),
                )
        finally:
            conn.close()

    with_retry(_write)


def _parse_hour(posted_at: Any) -> int:
    if isinstance(posted_at, datetime):
        return posted_at.astimezone(timezone.utc).hour
    if isinstance(posted_at, str):
        try:
            text = posted_at.replace("Z", "+00:00").replace(" ", "T", 1)
            return datetime.fromisoformat(text).astimezone(timezone.utc).hour
        except ValueError:
            return 0
    return 0


def _render_prompt(content: str, hour: int, topic_hint: str | None) -> str:
    """Substitute {content}/{post_hour_utc}/{topic_hint} without disturbing
    the JSON braces in the schema example."""
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template.replace("{content}", content)
        .replace("{post_hour_utc}", str(hour))
        .replace("{topic_hint}", topic_hint or "unknown")
    )


def _call_with_retry(prompt: str) -> dict[str, Any]:
    """Call LLM, parse JSON. One retry if parse / validate fails."""
    last_err: Exception | None = None
    for _ in range(2):
        try:
            raw = call_llm(prompt, response_format="json", max_retries=0)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise DistillError("LLM returned non-object JSON")
            return validate_distillation(payload)
        except (LLMError, json.JSONDecodeError, DistillError) as exc:
            last_err = exc
            continue
    assert last_err is not None
    raise DistillError(str(last_err))


def light_distill(observation_id: int) -> int | None:
    """Distill ONE observation. Returns the new entry id, or None on failure.

    Failures still stamp `distilled_at` so the nightly job doesn't endlessly
    retry the same broken post (UNIFIED_SPEC §6.1 anti-avalanche rule).
    """
    obs = _load_observation(observation_id)
    if obs is None:
        logger.warning("observation %d not found", observation_id)
        return None

    prompt = _render_prompt(
        obs["content"], _parse_hour(obs["posted_at"]), obs.get("topic_hint")
    )
    try:
        validated = _call_with_retry(prompt)
    except DistillError as exc:
        logger.warning("distill failed for obs %d: %s", observation_id, exc)
        _stamp_distilled(observation_id)
        return None

    validated.setdefault("post_hour_utc", _parse_hour(obs["posted_at"]))
    entry_id = upsert_entry(observation_id, validated)
    _stamp_distilled(observation_id)
    return entry_id


def full_distill(since: datetime) -> list[int]:
    """Backstop pass: distill every viral obs older than `since` lacking an entry."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM reaction_observations "
            "WHERE is_viral = 1 AND distilled_at IS NULL AND observed_at >= ?",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchall()
    finally:
        conn.close()

    new_ids: list[int] = []
    for row in rows:
        entry_id = light_distill(int(row["id"]))
        if entry_id is not None:
            new_ids.append(entry_id)
    return new_ids
