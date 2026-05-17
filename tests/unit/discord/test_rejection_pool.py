"""Unit tests for ❌ rejection + 🔄 revise handlers."""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.discord import rejection_pool, revise_handler
from tests.unit.discord.conftest import insert_draft


# --------------------------------------------------------------------------- #
# Rejection


@pytest.mark.asyncio
async def test_rejection_inserts_pool_row(
    tmp_db: sqlite3.Connection, stub_env: None
) -> None:
    draft_id = insert_draft(tmp_db, status="pushed_to_discord", message_id="m1")

    await rejection_pool.handle_rejection(draft_id, tmp_db, reason="off-topic")

    draft_row = tmp_db.execute(
        "SELECT status FROM drafts WHERE id=?", (draft_id,)
    ).fetchone()
    assert draft_row["status"] == "rejected"

    pool_row = tmp_db.execute(
        "SELECT draft_id, scorer_score, pattern_ids, reason "
        "FROM human_rejection_pool WHERE draft_id=?",
        (draft_id,),
    ).fetchone()
    assert pool_row is not None
    assert pool_row["draft_id"] == draft_id
    assert pool_row["reason"] == "off-topic"
    assert json.loads(pool_row["pattern_ids"]) == [1, 2]
    # No scorer table present yet → degraded score
    assert pool_row["scorer_score"] == 0


@pytest.mark.asyncio
async def test_rejection_skips_wrong_status(
    tmp_db: sqlite3.Connection, stub_env: None
) -> None:
    draft_id = insert_draft(tmp_db, status="published")
    await rejection_pool.handle_rejection(draft_id, tmp_db)
    pool_row = tmp_db.execute(
        "SELECT 1 FROM human_rejection_pool WHERE draft_id=?", (draft_id,)
    ).fetchone()
    assert pool_row is None


@pytest.mark.asyncio
async def test_rejection_unknown_draft_raises(
    tmp_db: sqlite3.Connection, stub_env: None
) -> None:
    with pytest.raises(ValueError):
        await rejection_pool.handle_rejection(31337, tmp_db)


@pytest.mark.asyncio
async def test_rejection_picks_up_scorer_score(
    tmp_db: sqlite3.Connection, stub_env: None
) -> None:
    # Simulate a scorer table existing with one row for this draft
    tmp_db.execute(
        "CREATE TABLE draft_scores (id INTEGER PRIMARY KEY, "
        "draft_id INTEGER, score_total INTEGER)"
    )
    draft_id = insert_draft(tmp_db, status="pushed_to_discord", message_id="m2")
    tmp_db.execute(
        "INSERT INTO draft_scores (draft_id, score_total) VALUES (?,?)",
        (draft_id, 84),
    )
    tmp_db.commit()

    await rejection_pool.handle_rejection(draft_id, tmp_db)
    pool_row = tmp_db.execute(
        "SELECT scorer_score FROM human_rejection_pool WHERE draft_id=?",
        (draft_id,),
    ).fetchone()
    assert pool_row["scorer_score"] == 84


# --------------------------------------------------------------------------- #
# Revise


@pytest.mark.asyncio
async def test_revise_resets_to_candidate(
    tmp_db: sqlite3.Connection, stub_env: None
) -> None:
    draft_id = insert_draft(
        tmp_db, status="pushed_to_discord", message_id="msg-revise"
    )
    await revise_handler.handle_revise(draft_id, tmp_db)
    row = tmp_db.execute(
        "SELECT status, discord_message_id FROM drafts WHERE id=?",
        (draft_id,),
    ).fetchone()
    assert row["status"] == "candidate"
    assert row["discord_message_id"] is None


@pytest.mark.asyncio
async def test_revise_skips_wrong_status(
    tmp_db: sqlite3.Connection, stub_env: None
) -> None:
    draft_id = insert_draft(tmp_db, status="candidate")
    await revise_handler.handle_revise(draft_id, tmp_db)
    row = tmp_db.execute(
        "SELECT status FROM drafts WHERE id=?", (draft_id,)
    ).fetchone()
    assert row["status"] == "candidate"
