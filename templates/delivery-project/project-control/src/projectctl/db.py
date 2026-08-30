"""Centralized SQLite connection management."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from projectctl.paths import DEFAULT_DB_PATH


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a projectctl-managed SQLite connection with foreign keys enabled."""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager that commits on success and always closes."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def journal_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).lower()


def foreign_keys_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    return int(row[0]) == 1
