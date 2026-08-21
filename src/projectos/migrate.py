"""Deterministic numbered SQL migrations for ProjectOS."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from projectos.db import connect, journal_mode
from projectos.paths import DEFAULT_DB_PATH, MIGRATIONS_DIR

_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$", re.IGNORECASE)


def list_migration_files(migrations_dir: Path | None = None) -> list[Path]:
    directory = migrations_dir if migrations_dir is not None else MIGRATIONS_DIR
    if not directory.is_dir():
        raise FileNotFoundError(f"Migrations directory not found: {directory}")
    files = [
        p for p in directory.iterdir() if p.is_file() and _MIGRATION_RE.match(p.name)
    ]
    files.sort(key=lambda p: (int(_MIGRATION_RE.match(p.name).group(1)), p.name))
    return files


def ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    ensure_schema_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {
        str(row["version"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows
    }


def apply_migrations(
    db_path: Path | str | None = None,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Apply pending migrations. Already-applied versions are skipped."""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(path)
    newly_applied: list[str] = []
    try:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"Failed to enable WAL mode; got {mode!r}")

        ensure_schema_migrations_table(conn)
        done = applied_versions(conn)

        for migration in list_migration_files(migrations_dir):
            version = migration.name
            if version in done:
                continue
            sql = migration.read_text(encoding="utf-8")
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (version,),
            )
            newly_applied.append(version)

        conn.commit()
        if journal_mode(conn) != "wal":
            raise RuntimeError("Database journal_mode is not WAL after migrations")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return newly_applied


def initialize_database(
    db_path: Path | str | None = None,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Create/initialize the database and apply all pending migrations."""
    return apply_migrations(db_path=db_path, migrations_dir=migrations_dir)
