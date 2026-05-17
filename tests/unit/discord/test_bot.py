"""Unit tests for src.discord.bot — push + poll loop."""
from __future__ import annotations

import sqlite3
from typing import Any

import httpx
import pytest

from src.discord import bot
from tests.unit.discord.conftest import insert_draft


# --------------------------------------------------------------------------- #
# env helpers


def test_require_env_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    # Bypass dotenv autoload by pointing it at a non-existent path
    monkeypatch.setattr(bot, "_SECRETS_PATH", bot._SECRETS_PATH.with_name("nope"))
    with pytest.raises(bot.DiscordConfigError):
        bot._get_token()


# --------------------------------------------------------------------------- #
# push_draft_to_discord


@pytest.mark.asyncio
async def test_push_draft_dry_run_updates_state(
    tmp_db: sqlite3.Connection,
    stub_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "1")
    draft_id = insert_draft(tmp_db)

    message_id = await bot.push_draft_to_discord(draft_id, conn=tmp_db)

    assert message_id == f"dryrun-{draft_id}"
    row = tmp_db.execute(
        "SELECT status, discord_message_id FROM drafts WHERE id=?", (draft_id,)
    ).fetchone()
    assert row["status"] == "pushed_to_discord"
    assert row["discord_message_id"] == message_id


@pytest.mark.asyncio
async def test_push_draft_calls_discord_api(
    tmp_db: sqlite3.Connection,
    stub_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: dict[str, Any] = {}
    reactions: list[str] = []

    async def fake_post_message(channel_id: str, content: str) -> dict[str, Any]:
        posted["channel_id"] = channel_id
        posted["content"] = content
        return {"id": "777111"}

    async def fake_put_reaction(
        channel_id: str, message_id: str, emoji: str
    ) -> None:
        reactions.append(emoji)

    monkeypatch.setattr(bot, "_post_message", fake_post_message)
    monkeypatch.setattr(bot, "_put_reaction", fake_put_reaction)

    draft_id = insert_draft(tmp_db)
    message_id = await bot.push_draft_to_discord(draft_id, conn=tmp_db)

    assert message_id == "777111"
    assert posted["channel_id"] == "1477872625526247577"
    assert f"Draft #{draft_id}" in posted["content"]
    assert reactions == list(bot.SEEDED_EMOJIS)


@pytest.mark.asyncio
async def test_push_draft_skips_when_not_candidate(
    tmp_db: sqlite3.Connection,
    stub_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def fake_post_message(*_: Any, **__: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"id": "x"}

    monkeypatch.setattr(bot, "_post_message", fake_post_message)
    draft_id = insert_draft(
        tmp_db, status="pushed_to_discord", message_id="existing-1"
    )

    result = await bot.push_draft_to_discord(draft_id, conn=tmp_db)
    assert result == "existing-1"
    assert called is False


@pytest.mark.asyncio
async def test_push_draft_unknown_id_raises(
    tmp_db: sqlite3.Connection, stub_env: None
) -> None:
    with pytest.raises(ValueError):
        await bot.push_draft_to_discord(99999, conn=tmp_db)


# --------------------------------------------------------------------------- #
# poll_reactions


@pytest.mark.asyncio
async def test_poll_dispatches_approve_handler(
    tmp_db: sqlite3.Connection,
    stub_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_id = insert_draft(
        tmp_db, status="pushed_to_discord", message_id="msg-approve"
    )

    async def fake_fetch(channel_id: str, message_id: str, emoji: str):
        if emoji == bot.APPROVE_EMOJI and message_id == "msg-approve":
            return [{"id": "9001", "username": "owner"}]
        return []

    called: dict[str, int] = {}

    async def fake_approve(did: int, conn: sqlite3.Connection) -> None:
        called["approve"] = did

    async def fake_reject(did: int, conn: sqlite3.Connection) -> None:
        called["reject"] = did

    async def fake_revise(did: int, conn: sqlite3.Connection) -> None:
        called["revise"] = did

    monkeypatch.setattr(bot, "_fetch_reaction_users", fake_fetch)

    async def fake_resolve() -> dict:
        return {
            bot.APPROVE_EMOJI: fake_approve,
            bot.REJECT_EMOJI: fake_reject,
            bot.REVISE_EMOJI: fake_revise,
        }

    monkeypatch.setattr(bot, "_resolve_handlers", fake_resolve)

    advanced = await bot.poll_reactions(conn=tmp_db)

    assert advanced == 1
    assert called == {"approve": draft_id}
    row = tmp_db.execute(
        "SELECT discord_reaction FROM drafts WHERE id=?", (draft_id,)
    ).fetchone()
    assert row["discord_reaction"] == bot.APPROVE_EMOJI


@pytest.mark.asyncio
async def test_poll_ignores_non_owner_reactions(
    tmp_db: sqlite3.Connection,
    stub_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert_draft(tmp_db, status="pushed_to_discord", message_id="msg-x")

    async def fake_fetch(channel_id: str, message_id: str, emoji: str):
        return [{"id": "1111", "username": "rando"}]

    monkeypatch.setattr(bot, "_fetch_reaction_users", fake_fetch)

    advanced = await bot.poll_reactions(conn=tmp_db)
    assert advanced == 0


@pytest.mark.asyncio
async def test_poll_skips_without_owner_env(
    tmp_db: sqlite3.Connection,
    stub_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISCORD_OWNER_USER_ID", raising=False)
    monkeypatch.setattr(bot, "_SECRETS_PATH", bot._SECRETS_PATH.with_name("nope"))
    insert_draft(tmp_db, status="pushed_to_discord", message_id="msg-y")

    advanced = await bot.poll_reactions(conn=tmp_db)
    assert advanced == 0


@pytest.mark.asyncio
async def test_poll_tolerates_http_errors(
    tmp_db: sqlite3.Connection,
    stub_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert_draft(tmp_db, status="pushed_to_discord", message_id="msg-err")

    async def boom(*_: Any, **__: Any) -> list:
        raise httpx.HTTPError("nope")

    async def fake_resolve() -> dict:
        return {e: _noop for e in bot.SEEDED_EMOJIS}

    async def _noop(did: int, conn: sqlite3.Connection) -> None:
        pass

    monkeypatch.setattr(bot, "_fetch_reaction_users", boom)
    monkeypatch.setattr(bot, "_resolve_handlers", fake_resolve)

    advanced = await bot.poll_reactions(conn=tmp_db)
    assert advanced == 0
