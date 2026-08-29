"""Durable next-action records for nonterminal runs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from projectos.store import utc_now_iso


def _new_action_id() -> str:
    return f"RNA-{uuid.uuid4().hex[:10].upper()}"


def ensure_run_next_actions_table(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_next_actions'"
    ).fetchone()
    if row is None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_next_actions (
                action_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                orchestration_job_id INTEGER,
                remediation_work_id TEXT,
                due_at TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )


def persist_run_next_action(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    project_id: str,
    action_type: str,
    orchestration_job_id: int | None = None,
    remediation_work_id: str | None = None,
    due_at: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    ensure_run_next_actions_table(conn)
    action_id = _new_action_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO run_next_actions (
            action_id, run_id, project_id, action_type, status,
            orchestration_job_id, remediation_work_id, due_at, payload_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id,
            run_id,
            project_id,
            action_type,
            orchestration_job_id,
            remediation_work_id,
            due_at,
            json.dumps(payload, sort_keys=True) if payload else None,
            now,
            now,
        ),
    )
    return action_id


def list_active_next_actions(
    conn: sqlite3.Connection,
    *,
    run_id: str,
) -> list[dict[str, Any]]:
    ensure_run_next_actions_table(conn)
    rows = conn.execute(
        """
        SELECT * FROM run_next_actions
        WHERE run_id = ? AND status IN ('pending', 'claimed')
        ORDER BY created_at ASC
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def complete_run_next_action(conn: sqlite3.Connection, *, action_id: str) -> None:
    ensure_run_next_actions_table(conn)
    conn.execute(
        """
        UPDATE run_next_actions
        SET status = 'completed', updated_at = ?
        WHERE action_id = ?
        """,
        (utc_now_iso(), action_id),
    )


def has_durable_next_action(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    project_id: str,
) -> bool:
    from projectos.execution_run import get_execution_run
    from projectos.remediation_recovery import list_outstanding_remediation_work
    from projectos.store import list_jobs_for_project

    run = get_execution_run(conn, run_id)
    if run is None:
        return False
    if run.status == "WAITING_FOR_SPONSOR":
        return True

    active_jobs = list_jobs_for_project(
        conn, project_id, statuses={"READY", "LEASED", "RUNNING", "QUEUED", "RETRY_WAIT"}
    )
    if active_jobs:
        return True

    outstanding = list_outstanding_remediation_work(conn, run_id=run_id)
    if outstanding:
        return True

    ensure_run_next_actions_table(conn)
    row = conn.execute(
        """
        SELECT 1 FROM run_next_actions
        WHERE run_id = ? AND status IN ('pending', 'claimed')
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    return row is not None
