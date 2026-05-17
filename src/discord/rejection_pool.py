"""❌ reaction → status=rejected + write human_rejection_pool row.

Per UNIFIED_SPEC §16.4: rejections are *kept* for analysis, never used as
direct downward weight signal. The scorer_score column captures what the
internal scorer thought of the draft so reviewer can study disagreement.
"""
from __future__ import annotations

import json
import logging
import sqlite3

LOGGER = logging.getLogger(__name__)


def _scorer_score_for_draft(draft_id: int, conn: sqlite3.Connection) -> int:
    """Best-effort: pull a score if a scorer table exists, else 0.

    The scorer subsystem may not yet be live — we degrade gracefully rather
    than crashing the gate.
    """
    try:
        row = conn.execute(
            "SELECT score_total FROM draft_scores WHERE draft_id=? "
            "ORDER BY id DESC LIMIT 1",
            (draft_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    return int(row["score_total"] or 0)


async def handle_rejection(
    draft_id: int,
    conn: sqlite3.Connection,
    *,
    reason: str | None = None,
) -> None:
    """Mark draft rejected and append to human_rejection_pool."""
    row = conn.execute(
        "SELECT pattern_ids, status FROM drafts WHERE id=?", (draft_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"draft {draft_id} not found")
    if row["status"] != "pushed_to_discord":
        LOGGER.info(
            "draft %s in status=%s, skip rejection", draft_id, row["status"]
        )
        return

    pattern_ids = row["pattern_ids"]
    try:
        json.loads(pattern_ids)  # validate; we re-store the original JSON string
    except (TypeError, ValueError):
        pattern_ids = json.dumps([])

    score = _scorer_score_for_draft(draft_id, conn)

    with conn:
        conn.execute(
            "UPDATE drafts SET status='rejected' WHERE id=?", (draft_id,)
        )
        conn.execute(
            "INSERT INTO human_rejection_pool "
            "(draft_id, scorer_score, pattern_ids, reason) VALUES (?,?,?,?)",
            (draft_id, score, pattern_ids, reason),
        )
    LOGGER.info("draft %s rejected (scorer_score=%s)", draft_id, score)
