"""Resume persisted remediation work after process interruption."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from projectos.domain_events import EventContext
from projectos.remediation_executor import RemediationExecutionResult, RemediationWorker, execute_remediation_work
from projectos.remediation_store import RemediationWorkRecord, list_remediation_work_for_run


@dataclass(frozen=True)
class RemediationRecoveryResult:
    resumed: int
    outcomes: tuple[RemediationExecutionResult, ...]


def list_outstanding_remediation_work(
    conn: sqlite3.Connection,
    *,
    run_id: str,
) -> list[RemediationWorkRecord]:
    return [
        work
        for work in list_remediation_work_for_run(conn, run_id)
        if work.status in {"ASSIGNED", "RUNNING"}
    ]


def resume_outstanding_remediation(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    project_id: str,
    repository_root: str,
    service_ctx=None,
    worker: RemediationWorker | None = None,
) -> RemediationRecoveryResult:
    """Resume incomplete remediation work without duplicating completed cycles."""
    if event_ctx.run_id is None:
        return RemediationRecoveryResult(resumed=0, outcomes=())
    outstanding = list_outstanding_remediation_work(conn, run_id=event_ctx.run_id)
    outcomes: list[RemediationExecutionResult] = []
    for work in outstanding:
        completed = conn.execute(
            """
            SELECT 1 FROM projectos_events
            WHERE run_id = ? AND event_type = 'WORK_COMPLETED'
              AND evidence_json LIKE ?
            LIMIT 1
            """,
            (event_ctx.run_id, f"%{work.work_item_id}%"),
        ).fetchone()
        if completed is not None:
            continue
        outcome = execute_remediation_work(
            conn,
            work=work,
            event_ctx=event_ctx,
            project_id=project_id,
            repository_root=repository_root,
            worker=worker,
            service_ctx=service_ctx,
        )
        outcomes.append(outcome)
    return RemediationRecoveryResult(resumed=len(outcomes), outcomes=tuple(outcomes))
