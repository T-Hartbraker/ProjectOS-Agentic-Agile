"""Durable ExecutionRun tracking."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

RUN_STATUSES = frozenset(
    {
        "PLANNING",
        "WAITING_APPROVAL",
        "WAITING_FOR_SPONSOR",
        "RUNNING",
        "BLOCKED",
        "FAILED",
        "COMPLETED",
        "CANCELLED",
        "ESCALATED",
    }
)


@dataclass(frozen=True)
class ExecutionRunRecord:
    run_id: str
    project_id: str
    handoff_id: str | None
    request_type: str
    objective: str
    status: str
    current_phase: str | None
    current_agent: str | None
    progress: int
    result_summary: str | None
    evidence_json: str | None
    started_at: str | None
    completed_at: str | None
    created_at: str


def _row_to_record(row: sqlite3.Row) -> ExecutionRunRecord:
    return ExecutionRunRecord(
        run_id=row["run_id"],
        project_id=row["project_id"],
        handoff_id=row["handoff_id"],
        request_type=row["request_type"],
        objective=row["objective"],
        status=row["status"],
        current_phase=row["current_phase"],
        current_agent=row["current_agent"],
        progress=int(row["progress"] or 0),
        result_summary=row["result_summary"],
        evidence_json=row["evidence_json"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        created_at=row["created_at"],
    )


def create_execution_run(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    handoff_id: str | None,
    request_type: str,
    objective: str,
) -> ExecutionRunRecord:
    run_id = f"RUN-{uuid.uuid4().hex[:8].upper()}"
    conn.execute(
        """
        INSERT INTO execution_runs (
            run_id, project_id, handoff_id, request_type, objective,
            status, current_phase, current_agent, progress, started_at
        ) VALUES (?, ?, ?, ?, ?, 'PLANNING', 'intake', 'PM Agent', 0, datetime('now'))
        """,
        (run_id, project_id, handoff_id, request_type, objective[:2000]),
    )
    row = conn.execute("SELECT * FROM execution_runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row is not None
    return _row_to_record(row)


def get_execution_run(conn: sqlite3.Connection, run_id: str) -> ExecutionRunRecord | None:
    row = conn.execute("SELECT * FROM execution_runs WHERE run_id = ?", (run_id,)).fetchone()
    return _row_to_record(row) if row else None


def update_execution_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str | None = None,
    current_phase: str | None = None,
    current_agent: str | None = None,
    progress: int | None = None,
    result_summary: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
        if status in {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED", "ESCALATED"}:
            fields.append("completed_at = datetime('now')")
    if current_phase is not None:
        fields.append("current_phase = ?")
        values.append(current_phase)
    if current_agent is not None:
        fields.append("current_agent = ?")
        values.append(current_agent)
    if progress is not None:
        fields.append("progress = ?")
        values.append(progress)
    if result_summary is not None:
        fields.append("result_summary = ?")
        values.append(result_summary[:4000])
    if evidence is not None:
        fields.append("evidence_json = ?")
        values.append(json.dumps(evidence, sort_keys=True)[:8000])
    if not fields:
        return
    values.append(run_id)
    conn.execute(
        f"UPDATE execution_runs SET {', '.join(fields)} WHERE run_id = ?",
        values,
    )
