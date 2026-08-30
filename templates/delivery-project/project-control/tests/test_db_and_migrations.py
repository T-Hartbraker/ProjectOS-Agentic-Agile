"""Database initialization and migration tests."""

from __future__ import annotations

from pathlib import Path

from projectctl.db import connect, foreign_keys_enabled, journal_mode
from projectctl.migrate import applied_versions, initialize_database

REQUIRED_TABLES = {
    "projects",
    "requirements",
    "acceptance_criteria",
    "epics",
    "features",
    "stories",
    "tasks",
    "iterations",
    "iteration_items",
    "releases",
    "defects",
    "test_cases",
    "test_runs",
    "risks",
    "issues",
    "assumptions",
    "decisions",
    "change_requests",
    "agents",
    "agent_assignments",
    "agent_runs",
    "artifacts",
    "trace_links",
    "token_ledger",
    "improvements",
    "custom_field_definitions",
    "custom_field_values",
    "audit_log",
    "schema_migrations",
}


def test_database_initialization_succeeds(tmp_path: Path) -> None:
    db = tmp_path / "init.db"
    applied = initialize_database(db_path=db)
    assert db.exists()
    assert len(applied) >= 1


def test_required_tables_are_created(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r["name"] for r in rows}
        missing = REQUIRED_TABLES - names
        assert not missing, f"Missing tables: {sorted(missing)}"
    finally:
        conn.close()


def test_journal_mode_is_wal_after_initialization(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        assert journal_mode(conn) == "wal"
    finally:
        conn.close()


def test_connection_factory_enables_foreign_keys(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        assert foreign_keys_enabled(conn) is True
    finally:
        conn.close()


def test_migrations_are_not_reapplied(tmp_path: Path) -> None:
    db = tmp_path / "migrate.db"
    first = initialize_database(db_path=db)
    assert first
    second = initialize_database(db_path=db)
    assert second == []

    conn = connect(db)
    try:
        versions = applied_versions(conn)
        for version in first:
            assert version in versions
        # Exactly one row per applied migration version
        count = conn.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()["c"]
        assert count == len(versions)
    finally:
        conn.close()
