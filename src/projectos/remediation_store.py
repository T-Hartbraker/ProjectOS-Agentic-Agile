"""Remediation work persistence — owner vs executor and durable sequencing."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from projectos.constants import QUEUE_TO_ROLE
from projectos.errors import OrchestrationError
from projectos.remediation_capability import resolve_remediation_execution
from projectos.store import create_job, utc_now_iso
from projectos.work_sequencing import (
    ensure_work_sequence_column,
    next_work_sequence,
    remediation_job_human_id,
)

AGENT_TO_QUEUE = {
    "developer-agent": "DELIVERY",
    "architecture-agent": "DELIVERY",
    "security-agent": "DELIVERY",
    "delivery-agent": "DELIVERY",
    "release-agent": "DELIVERY",
    "qa-agent": "DELIVERY",
    "pm-agent": "PM",
}


@dataclass(frozen=True)
class RemediationWorkRecord:
    work_item_id: str
    run_id: str
    project_id: str
    remediation_cycle: int
    finding_ids: tuple[str, ...]
    assigned_agent: str
    objective: str
    acceptance_criteria: str
    status: str
    source_candidate_id: str | None
    target_candidate_id: str | None
    orchestration_job_id: int | None
    result: dict[str, Any]
    work_sequence: int = 0
    finding_owner: str | None = None
    execution_queue: str | None = None


def _new_work_item_id() -> str:
    return f"RWK-{uuid.uuid4().hex[:10].upper()}"


def _row_to_record(row: sqlite3.Row) -> RemediationWorkRecord:
    finding_ids = []
    try:
        finding_ids = json.loads(str(row["finding_ids_json"] or "[]"))
    except json.JSONDecodeError:
        finding_ids = []
    result: dict[str, Any] = {}
    if row["result_json"]:
        try:
            result = json.loads(str(row["result_json"]))
        except json.JSONDecodeError:
            result = {}
    keys = row.keys()
    return RemediationWorkRecord(
        work_item_id=str(row["work_item_id"]),
        run_id=str(row["run_id"]),
        project_id=str(row["project_id"]),
        remediation_cycle=int(row["remediation_cycle"]),
        finding_ids=tuple(str(x) for x in finding_ids),
        assigned_agent=str(row["assigned_agent"]),
        objective=str(row["objective"]),
        acceptance_criteria=str(row["acceptance_criteria"] or ""),
        status=str(row["status"]),
        source_candidate_id=row["source_candidate_id"],
        target_candidate_id=row["target_candidate_id"],
        orchestration_job_id=int(row["orchestration_job_id"]) if row["orchestration_job_id"] else None,
        result=result,
        work_sequence=int(row["work_sequence"]) if "work_sequence" in keys else 0,
        finding_owner=row["finding_owner"] if "finding_owner" in keys else None,
        execution_queue=row["execution_queue"] if "execution_queue" in keys else None,
    )


def create_remediation_work(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    project_id: str,
    remediation_cycle: int,
    finding_ids: list[str],
    assigned_agent: str,
    objective: str,
    acceptance_criteria: str,
    source_candidate_id: str | None,
    repository_root: str,
    assignment_reason: str,
    findings: list[dict[str, Any]] | None = None,
    execution_queue: str | None = None,
    finding_owner: str | None = None,
) -> RemediationWorkRecord:
    ensure_work_sequence_column(conn)
    work_item_id = _new_work_item_id()
    primary = (findings or [{}])[0]
    owner, executor_agent, default_queue, _ = resolve_remediation_execution(primary)
    queue = execution_queue or default_queue or AGENT_TO_QUEUE.get(assigned_agent, "DELIVERY")
    role = QUEUE_TO_ROLE.get(queue, "DELIVERY")
    work_sequence = next_work_sequence(conn, run_id=run_id)
    job_human_id = remediation_job_human_id(
        run_id=run_id,
        work_sequence=work_sequence,
        assigned_agent=assigned_agent,
    )
    assignment = {
        "remediation_work_item_id": work_item_id,
        "run_id": run_id,
        "remediation_cycle": remediation_cycle,
        "work_sequence": work_sequence,
        "finding_ids": finding_ids,
        "assigned_agent": assigned_agent,
        "finding_owner": finding_owner or owner,
        "execution_queue": queue,
        "reason": assignment_reason,
        "source_candidate_id": source_candidate_id,
        "findings": findings or [],
        "objective": objective,
        "acceptance_criteria": acceptance_criteria,
    }
    job = create_job(
        conn,
        human_id=job_human_id,
        project_human_id=project_id,
        repository_root=repository_root,
        agent_role=role,
        queue=queue,
        status="READY",
        requires_worktree=queue in {"DELIVERY", "ARCHITECTURE", "INTEGRATION"},
        base_git_sha=source_candidate_id,
        assignment=assignment,
    )
    now = utc_now_iso()
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(remediation_work)").fetchall()}
    if "finding_owner" in cols and "execution_queue" in cols:
        conn.execute(
            """
            INSERT INTO remediation_work (
                work_item_id, run_id, project_id, remediation_cycle, work_sequence,
                finding_ids_json, assigned_agent, finding_owner, execution_queue,
                objective, acceptance_criteria, status, source_candidate_id,
                orchestration_job_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ASSIGNED', ?, ?, ?)
            """,
            (
                work_item_id,
                run_id,
                project_id,
                remediation_cycle,
                work_sequence,
                json.dumps(finding_ids, sort_keys=True),
                assigned_agent,
                finding_owner or owner,
                queue,
                objective[:2000],
                acceptance_criteria[:2000],
                source_candidate_id,
                job.id,
                now,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO remediation_work (
                work_item_id, run_id, project_id, remediation_cycle, finding_ids_json,
                assigned_agent, objective, acceptance_criteria, status, source_candidate_id,
                orchestration_job_id, created_at, work_sequence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ASSIGNED', ?, ?, ?, ?)
            """,
            (
                work_item_id,
                run_id,
                project_id,
                remediation_cycle,
                json.dumps(finding_ids, sort_keys=True),
                assigned_agent,
                objective[:2000],
                acceptance_criteria[:2000],
                source_candidate_id,
                job.id,
                now,
                work_sequence,
            ),
        )
    row = conn.execute(
        "SELECT * FROM remediation_work WHERE work_item_id = ?", (work_item_id,)
    ).fetchone()
    assert row is not None
    return _row_to_record(row)


def get_remediation_work(conn: sqlite3.Connection, work_item_id: str) -> RemediationWorkRecord | None:
    row = conn.execute(
        "SELECT * FROM remediation_work WHERE work_item_id = ?", (work_item_id,)
    ).fetchone()
    return _row_to_record(row) if row else None


def update_remediation_work(
    conn: sqlite3.Connection,
    work_item_id: str,
    *,
    status: str | None = None,
    target_candidate_id: str | None = None,
    result: dict[str, Any] | None = None,
) -> RemediationWorkRecord:
    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
        if status in {"COMPLETED", "FAILED", "BLOCKED"}:
            fields.append("completed_at = ?")
            values.append(utc_now_iso())
    if target_candidate_id is not None:
        fields.append("target_candidate_id = ?")
        values.append(target_candidate_id)
    if result is not None:
        fields.append("result_json = ?")
        values.append(json.dumps(result, sort_keys=True))
    if not fields:
        record = get_remediation_work(conn, work_item_id)
        if record is None:
            raise OrchestrationError(f"remediation work {work_item_id!r} not found")
        return record
    values.append(work_item_id)
    conn.execute(
        f"UPDATE remediation_work SET {', '.join(fields)} WHERE work_item_id = ?",
        values,
    )
    record = get_remediation_work(conn, work_item_id)
    if record is None:
        raise OrchestrationError(f"remediation work {work_item_id!r} not found")
    return record


def list_remediation_work_for_run(conn: sqlite3.Connection, run_id: str) -> list[RemediationWorkRecord]:
    rows = conn.execute(
        "SELECT * FROM remediation_work WHERE run_id = ? ORDER BY created_at ASC",
        (run_id,),
    ).fetchall()
    return [_row_to_record(row) for row in rows]


def count_remediation_cycles(conn: sqlite3.Connection, *, run_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT remediation_cycle) AS cycles
        FROM remediation_work
        WHERE run_id = ? AND status IN ('COMPLETED', 'FAILED', 'RUNNING', 'ASSIGNED')
        """,
        (run_id,),
    ).fetchone()
    return int(row["cycles"] or 0)
