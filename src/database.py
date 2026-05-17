"""Database layer for PepperBot.

Single entry point for connection handling, migrations, and a write-retry helper
for SQLITE_BUSY. All writes should wrap a `with conn:` block to use a transaction.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, TypeVar

from src.migrations.runner import (
    DEFAULT_DB_PATH,
    list_migration_files,
    run_migrations,
)

T = TypeVar("T")

DB_PATH: Path = DEFAULT_DB_PATH


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with WAL, FK on, Row factory."""
    target = db_path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: Path | None = None) -> list[str]:
    """Idempotent: apply any pending migrations. Returns newly-applied filenames."""
    return run_migrations(db_path or DB_PATH, verbose=False)


def load_schema_migrations(db_path: Path | None = None) -> list[str]:
    """Return ordered list of applied migration filenames."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT filename FROM schema_migrations ORDER BY filename"
        ).fetchall()
        return [r["filename"] for r in rows]
    finally:
        conn.close()


def with_retry(
    fn: Callable[[], T],
    retries: int = 3,
    backoff: float = 0.2,
) -> T:
    """Run `fn` retrying on sqlite3.OperationalError (SQLITE_BUSY/LOCKED)."""
    attempt = 0
    while True:
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            transient = "busy" in msg or "locked" in msg
            if not transient or attempt >= retries:
                raise
            time.sleep(backoff * (2 ** attempt))
            attempt += 1


def list_known_migrations() -> list[str]:
    return [p.name for p in list_migration_files()]
