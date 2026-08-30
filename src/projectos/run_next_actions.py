"""Durable next-action records for nonterminal runs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from projectos.errors import OrchestrationError
from projectos.store import list_jobs_for_run, utc_now_iso

_TERMINAL_JOB = frozenset({"SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"})
_TERMINAL_WORK = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED", "COMPLETED"})
_EXECUTABLE_JOB = frozenset({"READY", "LEASED", "RUNNING", "QUEUED", "RETRY_WAIT"})


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


def _validate_action_backing(
    conn: sqlite3.Connection,
    *,
    action_type: str,
    run_id: str,
    project_id: str,
    orchestration_job_id: int | None,
    remediation_work_id: str | None,
    due_at: str | None,
) -> None:
    from projectos.execution_run import get_execution_run

    run = get_execution_run(conn, run_id)
    if run is None:
        raise OrchestrationError(f"Next action requires execution run {run_id!r}")

    if action_type == "REMEDIATION_WORK":
        if not remediation_work_id:
            raise OrchestrationError("REMEDIATION_WORK next action requires remediation_work_id")
        row = conn.execute(
            "SELECT status, run_id FROM remediation_work WHERE work_item_id = ?",
            (remediation_work_id,),
        ).fetchone()
        if row is None:
            raise OrchestrationError(
                f"REMEDIATION_WORK next action references missing work {remediation_work_id!r}"
            )
        if str(row["run_id"]) != str(run_id):
            raise OrchestrationError("REMEDIATION_WORK next action run_id mismatch")
        if orchestration_job_id is not None:
            job = conn.execute(
                "SELECT run_id, status FROM orchestration_jobs WHERE id = ?",
                (orchestration_job_id,),
            ).fetchone()
            if job is None:
                raise OrchestrationError("REMEDIATION_WORK next action references missing job")
            if job["run_id"] and str(job["run_id"]) != str(run_id):
                raise OrchestrationError("REMEDIATION_WORK next action job run_id mismatch")
        return

    if action_type == "ACTIVE_ASSESSMENT":
        if orchestration_job_id is None:
            raise OrchestrationError("ACTIVE_ASSESSMENT next action requires orchestration_job_id")
        job = conn.execute(
            "SELECT run_id, status, source_candidate_sha FROM orchestration_jobs WHERE id = ?",
            (orchestration_job_id,),
        ).fetchone()
        if job is None:
            raise OrchestrationError("ACTIVE_ASSESSMENT next action references missing job")
        if job["run_id"] and str(job["run_id"]) != str(run_id):
            raise OrchestrationError("ACTIVE_ASSESSMENT next action job run_id mismatch")
        if str(job["status"]) in _TERMINAL_JOB:
            raise OrchestrationError("ACTIVE_ASSESSMENT next action references terminal job")
        if not job["source_candidate_sha"]:
            raise OrchestrationError("ACTIVE_ASSESSMENT next action requires source_candidate_sha")
        return

    if action_type == "PM_QUEUE":
        if orchestration_job_id is None:
            if str(run.status) == "WAITING_FOR_SPONSOR":
                return
            raise OrchestrationError("PM_QUEUE next action requires orchestration_job_id")
        job = conn.execute(
            "SELECT run_id, status FROM orchestration_jobs WHERE id = ?",
            (orchestration_job_id,),
        ).fetchone()
        if job is None:
            raise OrchestrationError("PM_QUEUE next action references missing job")
        if job["run_id"] and str(job["run_id"]) != str(run_id):
            raise OrchestrationError("PM_QUEUE next action job run_id mismatch")
        if str(job["status"]) in _TERMINAL_JOB:
            raise OrchestrationError("PM_QUEUE next action references terminal job")
        return

    if action_type in {"SCHEDULED_RETRY", "EXECUTABLE_JOB"}:
        if orchestration_job_id is None:
            raise OrchestrationError(f"{action_type} next action requires orchestration_job_id")
        job = conn.execute(
            "SELECT run_id, status FROM orchestration_jobs WHERE id = ?",
            (orchestration_job_id,),
        ).fetchone()
        if job is None:
            raise OrchestrationError(f"{action_type} next action references missing job")
        if job["run_id"] and str(job["run_id"]) != str(run_id):
            raise OrchestrationError(f"{action_type} next action job run_id mismatch")
        if str(job["status"]) in _TERMINAL_JOB:
            raise OrchestrationError(f"{action_type} next action references terminal job")
        if action_type == "SCHEDULED_RETRY" and str(job["status"]) != "RETRY_WAIT" and not due_at:
            raise OrchestrationError("SCHEDULED_RETRY next action requires RETRY_WAIT job or due_at")
        return

    raise OrchestrationError(f"Unsupported next action type {action_type!r}")


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
    """Create a backed next-action row; rejects structurally invalid actions."""
    ensure_run_next_actions_table(conn)
    _validate_action_backing(
        conn,
        action_type=action_type,
        run_id=run_id,
        project_id=project_id,
        orchestration_job_id=orchestration_job_id,
        remediation_work_id=remediation_work_id,
        due_at=due_at,
    )
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


create_run_next_action = persist_run_next_action


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
    return [dict(row) for row in rows if _next_action_is_live(conn, dict(row))]


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


def cancel_run_next_action(conn: sqlite3.Connection, *, action_id: str) -> None:
    ensure_run_next_actions_table(conn)
    conn.execute(
        """
        UPDATE run_next_actions
        SET status = 'cancelled', updated_at = ?
        WHERE action_id = ?
        """,
        (utc_now_iso(), action_id),
    )


def complete_run_next_action_for_job(
    conn: sqlite3.Connection,
    *,
    orchestration_job_id: int,
) -> None:
    ensure_run_next_actions_table(conn)
    conn.execute(
        """
        UPDATE run_next_actions
        SET status = 'completed', updated_at = ?
        WHERE orchestration_job_id = ?
          AND status IN ('pending', 'claimed')
        """,
        (utc_now_iso(), orchestration_job_id),
    )


def complete_run_next_action_for_work(
    conn: sqlite3.Connection,
    *,
    remediation_work_id: str,
) -> None:
    ensure_run_next_actions_table(conn)
    conn.execute(
        """
        UPDATE run_next_actions
        SET status = 'completed', updated_at = ?
        WHERE remediation_work_id = ?
          AND status IN ('pending', 'claimed')
        """,
        (utc_now_iso(), remediation_work_id),
    )


def _next_action_is_live(conn: sqlite3.Connection, action: dict[str, Any]) -> bool:
    action_type = str(action.get("action_type") or "")
    run_id = str(action.get("run_id") or "")
    from projectos.execution_run import get_execution_run

    run = get_execution_run(conn, run_id)
    job_id = action.get("orchestration_job_id")

    if action_type == "PM_QUEUE":
        payload = action.get("payload_json")
        sponsor_wait = False
        if payload:
            try:
                sponsor_wait = bool(json.loads(str(payload)).get("sponsor_wait"))
            except json.JSONDecodeError:
                sponsor_wait = False
        if sponsor_wait and run is not None and str(run.status) == "WAITING_FOR_SPONSOR":
            return True

    if job_id is not None:
        row = conn.execute(
            "SELECT status, run_id FROM orchestration_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if row is None:
            return False
        if row["run_id"] and str(row["run_id"]) != run_id:
            return False
        if str(row["status"]) in _TERMINAL_JOB:
            return False
        if action_type == "ACTIVE_ASSESSMENT" and str(row["status"]) not in _EXECUTABLE_JOB:
            return False
        if action_type == "SCHEDULED_RETRY" and str(row["status"]) not in {
            "RETRY_WAIT",
            "READY",
            "LEASED",
            "RUNNING",
        }:
            return False
    elif action_type in {"ACTIVE_ASSESSMENT", "SCHEDULED_RETRY", "EXECUTABLE_JOB", "PM_QUEUE"}:
        return False

    work_id = action.get("remediation_work_id")
    if work_id:
        row = conn.execute(
            "SELECT status, run_id FROM remediation_work WHERE work_item_id = ?",
            (str(work_id),),
        ).fetchone()
        if row is None:
            return False
        if str(row["run_id"]) != run_id:
            return False
        if str(row["status"]) in _TERMINAL_WORK:
            return False
    elif action_type == "REMEDIATION_WORK":
        return False

    if action_type == "REMEDIATION_WORK":
        return bool(work_id)
    if action_type in {"ACTIVE_ASSESSMENT", "SCHEDULED_RETRY", "EXECUTABLE_JOB", "PM_QUEUE"}:
        return job_id is not None
    return False


def reconcile_run_next_actions(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    project_id: str,
) -> list[str]:
    """Complete stale actions and ensure a single executable next action when possible."""
    from projectos.execution_run import get_execution_run
    from projectos.run_outcomes import is_terminal_run_status
    from projectos.store import list_jobs_for_run

    ensure_run_next_actions_table(conn)
    run = get_execution_run(conn, run_id)
    if run is None or is_terminal_run_status(run.status):
        return []

    actions = conn.execute(
        """
        SELECT action_id, orchestration_job_id, status
        FROM run_next_actions
        WHERE run_id = ? AND status IN ('pending', 'claimed')
        ORDER BY created_at ASC
        """,
        (run_id,),
    ).fetchall()
    reconciled: list[str] = []
    for row in actions:
        job_id = row["orchestration_job_id"]
        if job_id is None:
            continue
        job = conn.execute(
            "SELECT status FROM orchestration_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if job is not None and str(job["status"]) in _TERMINAL_JOB:
            complete_run_next_action(conn, action_id=str(row["action_id"]))
            reconciled.append(str(row["action_id"]))

    if has_durable_next_action(conn, run_id=run_id, project_id=project_id):
        return reconciled

    ready_jobs = list_jobs_for_run(conn, run_id, statuses=_EXECUTABLE_JOB)
    if not ready_jobs:
        return reconciled
    next_job = sorted(ready_jobs, key=lambda j: (j.priority, j.id))[0]
    action_id = persist_run_next_action(
        conn,
        run_id=run_id,
        project_id=project_id,
        action_type="EXECUTABLE_JOB",
        orchestration_job_id=next_job.id,
        payload={"job_human_id": next_job.human_id, "reconciled": True},
    )
    reconciled.append(action_id)
    return reconciled


def has_durable_next_action(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    project_id: str,
) -> bool:
    from projectos.execution_run import get_execution_run
    from projectos.remediation_recovery import list_outstanding_remediation_work

    run = get_execution_run(conn, run_id)
    if run is None:
        return False
    if run.status == "WAITING_FOR_SPONSOR":
        return True

    active_jobs = list_jobs_for_run(conn, run_id, statuses=_EXECUTABLE_JOB)
    if active_jobs:
        return True

    outstanding = list_outstanding_remediation_work(conn, run_id=run_id)
    if outstanding:
        return True

    return bool(list_active_next_actions(conn, run_id=run_id))
