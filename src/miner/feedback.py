"""Outcome feedback → EMA-update technique success scores (UNIFIED_SPEC §6.5, §16.4).

Called by reviewer once per post lifecycle:
- "top"    → α = 0.3, target 100   (reward winners)
- "mid"    → no-op (steady state)
- "bottom" → α = 0.1, target 0     (gentle decay; we never zero a pattern out
                                    immediately, that's what pattern_cooling is for)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from src.database import get_conn, with_retry

__all__ = [
    "apply_post_outcome",
    "EMA_ALPHA",
    "EMA_ALPHA_TOP",
    "EMA_ALPHA_BOTTOM",
    "COOLING_MISS_THRESHOLD",
    "COOLING_RESET_DAYS",
    "Outcome",
]

logger = logging.getLogger(__name__)

EMA_ALPHA = 0.2
EMA_ALPHA_TOP = 0.3
EMA_ALPHA_BOTTOM = 0.1
COOLING_MISS_THRESHOLD = 3
COOLING_RESET_DAYS = 7

Outcome = Literal["top", "mid", "bottom"]


def _pattern_ids_for_draft(draft_id: int) -> list[int]:
    """Pull the pattern ids the writer baked into this draft.

    Round-3 fix: prior version joined ``retrieval_log.post_id`` which is
    never populated in manual mode → returned [] always → learning loop
    silently dead. ``drafts.pattern_ids`` is the canonical source written
    by ``writer._persist_draft`` at draft creation time.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT pattern_ids FROM drafts WHERE id = ?",
            (draft_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return []
    try:
        ids = json.loads(row["pattern_ids"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [int(i) for i in ids if int(i) >= 0]


def _ema(old: float, target: float, alpha: float) -> float:
    return (1.0 - alpha) * old + alpha * target


def _bump_cooling(entry_id: int) -> None:
    """Increment miss counter; cool the pattern once we cross the threshold."""
    reset_after = datetime.now(timezone.utc) + timedelta(days=COOLING_RESET_DAYS)

    def _write() -> None:
        conn = get_conn()
        try:
            with conn:
                existing = conn.execute(
                    "SELECT consecutive_misses FROM pattern_cooling "
                    "WHERE pattern_id = ?",
                    (entry_id,),
                ).fetchone()
                misses = (int(existing["consecutive_misses"]) if existing else 0) + 1
                if existing is None:
                    conn.execute(
                        "INSERT INTO pattern_cooling "
                        "(pattern_id, reset_after, consecutive_misses) "
                        "VALUES (?, ?, ?)",
                        (
                            entry_id,
                            reset_after.isoformat(),
                            misses,
                        ),
                    )
                else:
                    if misses >= COOLING_MISS_THRESHOLD:
                        conn.execute(
                            "UPDATE pattern_cooling SET "
                            "consecutive_misses = ?, "
                            "cooled_at = CURRENT_TIMESTAMP, "
                            "reset_after = ? WHERE pattern_id = ?",
                            (misses, reset_after.isoformat(), entry_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE pattern_cooling SET consecutive_misses = ? "
                            "WHERE pattern_id = ?",
                            (misses, entry_id),
                        )
        finally:
            conn.close()

    with_retry(_write)


def _reset_cooling(entry_id: int) -> None:
    """Clear miss counter on a winning pattern."""

    def _write() -> None:
        conn = get_conn()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM pattern_cooling WHERE pattern_id = ?",
                    (entry_id,),
                )
        finally:
            conn.close()

    with_retry(_write)


def apply_post_outcome(
    draft_id: int,
    outcome: Outcome,
    ema_alpha: float | None = None,
) -> None:
    """EMA-shift success_score for every entry the writer used, then bump cooling.

    NOTE: the param is the draft_id (the canonical id the rest of the pipeline
    uses); pattern lookup goes via ``drafts.pattern_ids``, NOT
    ``retrieval_log.post_id`` (which is never populated in manual mode).
    """
    entry_ids = _pattern_ids_for_draft(draft_id)
    if not entry_ids:
        logger.info("no pattern_ids on draft=%s; skipping outcome update", draft_id)
        return

    if outcome == "top":
        alpha = ema_alpha if ema_alpha is not None else EMA_ALPHA_TOP
        target = 100.0
    elif outcome == "bottom":
        alpha = ema_alpha if ema_alpha is not None else EMA_ALPHA_BOTTOM
        target = 0.0
    else:  # "mid" — neutral, just bump usage counter
        _bump_usage(entry_ids)
        return

    def _write() -> None:
        conn = get_conn()
        try:
            with conn:
                for eid in entry_ids:
                    row = conn.execute(
                        "SELECT success_score FROM technique_entries WHERE id = ?",
                        (eid,),
                    ).fetchone()
                    if row is None:
                        continue
                    new_score = _ema(float(row["success_score"]), target, alpha)
                    conn.execute(
                        "UPDATE technique_entries SET "
                        "success_score = ?, "
                        "times_used_in_post = times_used_in_post + 1 "
                        "WHERE id = ?",
                        (new_score, eid),
                    )
        finally:
            conn.close()

    with_retry(_write)

    for eid in entry_ids:
        if outcome == "top":
            _reset_cooling(eid)
        elif outcome == "bottom":
            _bump_cooling(eid)


def _bump_usage(entry_ids: list[int]) -> None:
    if not entry_ids:
        return
    placeholders = ",".join("?" for _ in entry_ids)

    def _write() -> None:
        conn = get_conn()
        try:
            with conn:
                conn.execute(
                    f"UPDATE technique_entries SET "
                    f"times_used_in_post = times_used_in_post + 1 "
                    f"WHERE id IN ({placeholders})",
                    tuple(entry_ids),
                )
        finally:
            conn.close()

    with_retry(_write)
