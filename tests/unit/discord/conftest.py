"""Shared fixtures for S13 Discord tests.

Builds an isolated SQLite DB with the full schema (all five migrations applied)
and stubs Discord env so no real network is ever touched.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database import get_conn  # noqa: E402
from src.migrations.runner import run_migrations  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    run_migrations(db_path, verbose=False)
    conn = get_conn(db_path)
    yield conn
    conn.close()


@pytest.fixture()
def stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token-xxx")
    monkeypatch.setenv("DISCORD_DRAFT_CHANNEL_ID", "1477872625526247577")
    monkeypatch.setenv("DISCORD_OWNER_USER_ID", "9001")
    monkeypatch.delenv("DRY_RUN", raising=False)


def insert_draft(
    conn: sqlite3.Connection,
    *,
    content: str = "盘前快评：纳指期货跌 0.3%",
    status: str = "candidate",
    message_id: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO drafts (content, content_length, content_mode, "
        "optimal_length, topic_lane, persona, pattern_ids, "
        "source_observation_ids, status, discord_message_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            content,
            len(content),
            "insight",
            "short",
            "macro",
            "pepper",
            json.dumps([1, 2]),
            json.dumps([42]),
            status,
            message_id,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)
