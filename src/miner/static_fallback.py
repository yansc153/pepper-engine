"""Parse `templates/hooks_finance.md` markdown tables into in-memory entries.

Used as the cold-start corpus when `technique_entries` is empty or sparse.
Static rows live in negative-id space (-1, -2, ...) and never persist to SQL.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from src.miner.types import TechniqueEntry

__all__ = ["load_static_entries", "filter_static", "STATIC_TEMPLATE_PATH"]

STATIC_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "templates" / "hooks_finance.md"
)

# Map persona names from the markdown to UNIFIED_SPEC persona keys.
_PERSONA_ALIAS: dict[str, str] = {
    "finance_neutral": "finance_neutral",
    "finance_contrarian": "finance_contrarian",
    "finance_macro": "finance_macro",
    "finance_skeptical": "finance_contrarian",
    "finance_analytical": "finance_neutral",
    "finance_empathetic": "finance_neutral",
    "finance_reflective": "finance_neutral",
    "general_observer": "general_observer",
}

_LANE_ALIAS: dict[str, str] = {
    "pre_market": "pre_market",
    "intraday": "intraday",
    "post_market": "post_market",
    "overnight": "overnight",
    "general": "general_tech_ai",
    "general_tech_ai": "general_tech_ai",
    "general_meme_career": "general_meme_career",
}

_HEADER_RE = re.compile(
    r"\|\s*hook_pattern\s*\|\s*hook_example\s*\|\s*topic_lane\s*\|",
    re.IGNORECASE,
)


def _parse_hour(raw: str) -> int:
    raw = raw.strip()
    if raw in {"*", ""}:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _parse_persona(raw: str) -> str:
    key = raw.strip()
    return _PERSONA_ALIAS.get(key, "finance_neutral")


def _parse_lane(raw: str) -> str:
    return _LANE_ALIAS.get(raw.strip(), "other")


def _parse_table(text: str) -> list[list[str]]:
    """Extract every markdown table row that follows a 'hook_pattern' header."""
    rows: list[list[str]] = []
    lines = text.splitlines()
    in_table = False
    for line in lines:
        stripped = line.strip()
        if _HEADER_RE.search(stripped):
            in_table = True
            continue
        if in_table:
            if not stripped.startswith("|"):
                in_table = False
                continue
            # skip the separator row |---|---|
            if set(stripped.replace("|", "").replace(" ", "").replace(":", "")) <= {"-"}:
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) >= 6:
                rows.append(cells)
    return rows


@lru_cache(maxsize=1)
def load_static_entries(
    path: str | None = None,
) -> tuple[TechniqueEntry, ...]:
    """Load static hook templates as immutable in-memory entries.

    Cached: parsing is cheap but called on every cold-start retrieve.
    """
    target = Path(path) if path else STATIC_TEMPLATE_PATH
    if not target.exists():
        return ()
    text = target.read_text(encoding="utf-8")
    rows = _parse_table(text)
    now = datetime.now(timezone.utc)
    entries: list[TechniqueEntry] = []
    next_id = -1
    for row in rows:
        hook_pattern, hook_example, lane_raw, hour_raw, persona_raw, stance_raw = row[:6]
        try:
            stance = int(stance_raw)
        except ValueError:
            stance = 3
        lane = _parse_lane(lane_raw)
        persona = _parse_persona(persona_raw)
        hour = _parse_hour(hour_raw)
        entries.append(
            TechniqueEntry(
                id=next_id,
                observation_id=0,
                hook_pattern=hook_pattern,
                hook_example=hook_example,
                syntax_signature="short_comma_no_period",
                sentence_len_avg=20.0,
                sentence_len_p90=30.0,
                stance_strength=stance,
                emotion_triggers=["共情"],
                image_style="none",
                post_hour_utc=hour,
                topic_lane=lane,
                applicable_personas=[persona],
                content_mode="insight",
                optimal_length="short",
                distilled_at=now,
                success_score=50.0,
                times_retrieved=0,
                times_used_in_post=0,
                recency_weight=1.0,
            )
        )
        next_id -= 1
    return tuple(entries)


def filter_static(
    entries: tuple[TechniqueEntry, ...],
    *,
    topic_lane: str,
    post_hour_utc: int,
    persona: str,
    avoid: set[int],
    hour_window: int = 2,
    k: int = 5,
) -> list[TechniqueEntry]:
    """Dict-style filter — no SQL, used when SQL corpus is empty / sparse."""
    matched: list[TechniqueEntry] = []
    for e in entries:
        if e.id in avoid:
            continue
        if e.topic_lane != topic_lane:
            continue
        if persona not in e.applicable_personas:
            continue
        if e.post_hour_utc != 0 and abs(e.post_hour_utc - post_hour_utc) > hour_window:
            continue
        matched.append(e)
        if len(matched) >= k:
            break
    return matched
