"""Unit tests for ✅ approval → publisher → published."""
from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from src.discord import publisher_callback
from tests.unit.discord.conftest import insert_draft


@pytest.mark.asyncio
async def test_approval_calls_publisher_and_sets_url(
    tmp_db: sqlite3.Connection,
    stub_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_APPROVAL_MODE", "auto")
    seen: dict[str, Any] = {}

    async def fake_post(content: str, image_path: str | None) -> str:
        seen["content"] = content
        seen["image"] = image_path
        return "https://x.com/pepper/status/123"

    monkeypatch.setattr(publisher_callback, "_post_tweet", fake_post)

    draft_id = insert_draft(tmp_db, status="pushed_to_discord", message_id="m1")
    await publisher_callback.handle_approval(draft_id, tmp_db)

    row = tmp_db.execute(
        "SELECT status, tweet_url, posted_at FROM drafts WHERE id=?",
        (draft_id,),
    ).fetchone()
    assert row["status"] == "published"
    assert row["tweet_url"] == "https://x.com/pepper/status/123"
    assert row["posted_at"] is not None
    assert seen["content"].startswith("盘前快评")


@pytest.mark.asyncio
async def test_approval_skips_wrong_status(
    tmp_db: sqlite3.Connection,
    stub_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_post(content: str, image_path: str | None) -> str:
        nonlocal calls
        calls += 1
        return "x"

    monkeypatch.setattr(publisher_callback, "_post_tweet", fake_post)

    draft_id = insert_draft(tmp_db, status="candidate")
    await publisher_callback.handle_approval(draft_id, tmp_db)
    assert calls == 0


@pytest.mark.asyncio
async def test_approval_unknown_draft_raises(
    tmp_db: sqlite3.Connection, stub_env: None
) -> None:
    with pytest.raises(ValueError):
        await publisher_callback.handle_approval(424242, tmp_db)


@pytest.mark.asyncio
async def test_approval_leaves_at_approved_on_empty_url(
    tmp_db: sqlite3.Connection,
    stub_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_APPROVAL_MODE", "auto")

    async def fake_post(content: str, image_path: str | None) -> str:
        return ""

    monkeypatch.setattr(publisher_callback, "_post_tweet", fake_post)
    draft_id = insert_draft(tmp_db, status="pushed_to_discord", message_id="m2")
    await publisher_callback.handle_approval(draft_id, tmp_db)

    row = tmp_db.execute(
        "SELECT status, tweet_url FROM drafts WHERE id=?", (draft_id,)
    ).fetchone()
    assert row["status"] == "approved"
    assert row["tweet_url"] is None


@pytest.mark.asyncio
async def test_approval_retry_from_approved_state(
    tmp_db: sqlite3.Connection,
    stub_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_APPROVAL_MODE", "auto")

    async def fake_post(content: str, image_path: str | None) -> str:
        return "https://x.com/pepper/status/999"

    monkeypatch.setattr(publisher_callback, "_post_tweet", fake_post)
    draft_id = insert_draft(tmp_db, status="pushed_to_discord", message_id="m3")
    # First call: simulate publisher succeeding directly
    with tmp_db:
        tmp_db.execute(
            "UPDATE drafts SET status='approved' WHERE id=?", (draft_id,)
        )
    await publisher_callback.handle_approval(draft_id, tmp_db)
    row = tmp_db.execute(
        "SELECT status, tweet_url FROM drafts WHERE id=?", (draft_id,)
    ).fetchone()
    assert row["status"] == "published"
    assert row["tweet_url"].endswith("999")


@pytest.mark.asyncio
async def test_approval_manual_mode_stops_at_approved(
    tmp_db: sqlite3.Connection,
    stub_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default DISCORD_APPROVAL_MODE=manual: ✅ only marks approved,
    publisher NEVER called. self_monitor will bind tweet_url later."""
    monkeypatch.delenv("DISCORD_APPROVAL_MODE", raising=False)
    calls = 0

    async def fake_post(content: str, image_path: str | None) -> str:
        nonlocal calls
        calls += 1
        return "https://x.com/should/not/happen"

    monkeypatch.setattr(publisher_callback, "_post_tweet", fake_post)
    draft_id = insert_draft(tmp_db, status="pushed_to_discord", message_id="m4")
    await publisher_callback.handle_approval(draft_id, tmp_db)

    assert calls == 0, "manual mode must NOT call publisher"
    row = tmp_db.execute(
        "SELECT status, tweet_url, posted_at FROM drafts WHERE id=?",
        (draft_id,),
    ).fetchone()
    assert row["status"] == "approved"
    assert row["tweet_url"] is None
    assert row["posted_at"] is None
