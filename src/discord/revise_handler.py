"""🔄 reaction → reset draft to candidate so writer regenerates next loop.

We clear ``discord_message_id`` so the next push creates a fresh Discord message
(the old one stays in the channel as historical record but is no longer the
authoritative gate). ``discord_reaction`` is kept so reviewer can correlate
"revised after seeing this version".
"""
from __future__ import annotations

import logging
import sqlite3

LOGGER = logging.getLogger(__name__)


async def handle_revise(draft_id: int, conn: sqlite3.Connection) -> None:
    """Send draft back to candidate so writer regenerates content."""
    row = conn.execute(
        "SELECT status FROM drafts WHERE id=?", (draft_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"draft {draft_id} not found")
    if row["status"] != "pushed_to_discord":
        LOGGER.info(
            "draft %s in status=%s, skip revise", draft_id, row["status"]
        )
        return

    with conn:
        conn.execute(
            "UPDATE drafts SET status='candidate', discord_message_id=NULL "
            "WHERE id=?",
            (draft_id,),
        )
    LOGGER.info("draft %s sent back to candidate for regeneration", draft_id)
