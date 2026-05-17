"""Shared fixtures for miner unit tests.

`tmp_db` builds an isolated SQLite, runs all migrations, and re-points
`src.database.DB_PATH` + `src.miner.*` modules at it so the production code
under test uses the temp DB without any signature changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from src import database
from src.database import init_db


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    p = tmp_path / "miner.db"
    init_db(p)
    # All miner code calls get_conn() with no args → relies on database.DB_PATH.
    monkeypatch.setattr(database, "DB_PATH", p)
    yield p


@pytest.fixture()
def seed_50(tmp_db: Path) -> Path:
    sql = Path(__file__).resolve().parents[2] / "fixtures" / "db" / "seed_50_entries.sql"
    _load_sql(tmp_db, sql)
    return tmp_db


@pytest.fixture()
def seed_500(tmp_db: Path) -> Path:
    sql = Path(__file__).resolve().parents[2] / "fixtures" / "db" / "seed_500_entries.sql"
    _load_sql(tmp_db, sql)
    return tmp_db


def _load_sql(db_path: Path, sql_file: Path) -> None:
    from src.database import get_conn

    text = sql_file.read_text(encoding="utf-8")
    conn = get_conn(db_path)
    try:
        with conn:
            conn.executescript(text)
    finally:
        conn.close()
