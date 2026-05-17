"""Migrations runner. Executes *.sql under this dir in filename order, idempotent."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = MIGRATIONS_DIR.parent.parent / "data" / "pepperbot.db"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename TEXT PRIMARY KEY,
  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def _open(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def list_migration_files() -> list[Path]:
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))


def applied_set(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {r["filename"] for r in rows}


def run_migrations(db_path: Path = DEFAULT_DB_PATH, verbose: bool = True) -> list[str]:
    """Apply pending migrations. Returns list of newly applied filenames."""
    conn = _open(db_path)
    try:
        with conn:
            conn.execute(SCHEMA_MIGRATIONS_DDL)
        already = applied_set(conn)
        applied: list[str] = []
        for path in list_migration_files():
            name = path.name
            if name in already:
                if verbose:
                    print(f"[skip] {name} already applied")
                continue
            sql = path.read_text(encoding="utf-8")
            with conn:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations(filename) VALUES (?)", (name,)
                )
            applied.append(name)
            if verbose:
                print(f"[apply] {name}")
        return applied
    finally:
        conn.close()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH
    new = run_migrations(target)
    print(f"Done. {len(new)} new migration(s) applied to {target}.")
