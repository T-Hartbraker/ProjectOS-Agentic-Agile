"""Collision-safe durable work item sequencing."""

from __future__ import annotations

import sqlite3

from projectos.store import utc_now_iso


def next_work_sequence(conn: sqlite3.Connection, *, run_id: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(work_sequence), 0) AS max_seq
        FROM remediation_work
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return int(row["max_seq"] or 0) + 1


def remediation_job_human_id(
    *,
    run_id: str,
    work_sequence: int,
    assigned_agent: str,
) -> str:
    agent_slug = assigned_agent.replace("-agent", "").replace("_", "-")
    return f"{run_id}__WORK_{work_sequence:04d}__{agent_slug}"


def ensure_work_sequence_column(conn: sqlite3.Connection) -> None:
    """Idempotent column ensure for tests on pre-migration DBs."""
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(remediation_work)").fetchall()}
    if "work_sequence" not in cols:
        conn.execute(
            "ALTER TABLE remediation_work ADD COLUMN work_sequence INTEGER NOT NULL DEFAULT 0"
        )
