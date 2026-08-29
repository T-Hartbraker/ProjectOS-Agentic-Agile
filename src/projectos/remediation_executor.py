"""Execute remediation work through the authoritative worker backbone."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from projectos.candidate_model import (
    CANDIDATE_TYPE_GIT_SHA,
    set_run_active_candidate,
    validate_candidate_identity,
)
from projectos.domain_events import EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.remediation_store import RemediationWorkRecord, update_remediation_work
from projectos.store import get_job, mark_succeeded


@dataclass(frozen=True)
class RemediationExecutionResult:
    work_item_id: str
    status: str
    target_candidate_id: str | None
    evidence: dict[str, Any]
    candidate_type: str = CANDIDATE_TYPE_GIT_SHA


class RemediationWorker(Protocol):
    def __call__(
        self,
        conn: sqlite3.Connection,
        *,
        work: RemediationWorkRecord,
        event_ctx: EventContext,
        project_id: str,
        repository_root: str,
    ) -> RemediationExecutionResult: ...


def production_remediation_worker(
    conn: sqlite3.Connection,
    *,
    work: RemediationWorkRecord,
    event_ctx: EventContext,
    project_id: str,
    repository_root: str,
    service_ctx,
) -> RemediationExecutionResult:
    """Invoke orchestration job via WorkerService (production path only)."""
    if work.orchestration_job_id is None:
        raise OrchestrationError("Remediation work missing orchestration job")
    job = get_job(conn, work.orchestration_job_id)
    if job is None:
        raise OrchestrationError(f"Remediation job id={work.orchestration_job_id} not found")

    from projectos.services.facades import WorkerService

    outcome = WorkerService(service_ctx).run_once(job_human_id=job.human_id)
    if outcome.status != "SUCCEEDED":
        return RemediationExecutionResult(
            work_item_id=work.work_item_id,
            status="FAILED",
            target_candidate_id=None,
            evidence={"worker_status": outcome.status, "message": outcome.message},
        )
    refreshed = get_job(conn, work.orchestration_job_id)
    target = refreshed.candidate_git_sha if refreshed else None
    candidate_type = CANDIDATE_TYPE_GIT_SHA
    if not target:
        return RemediationExecutionResult(
            work_item_id=work.work_item_id,
            status="FAILED",
            target_candidate_id=None,
            evidence={"reason": "missing_candidate", "job_human_id": job.human_id},
        )
    try:
        validate_candidate_identity(
            target,
            candidate_type=candidate_type,
            repository_root=repository_root,
        )
    except OrchestrationError as exc:
        return RemediationExecutionResult(
            work_item_id=work.work_item_id,
            status="FAILED",
            target_candidate_id=None,
            evidence={"reason": "invalid_candidate", "message": str(exc)},
        )
    return RemediationExecutionResult(
        work_item_id=work.work_item_id,
        status="COMPLETED",
        target_candidate_id=target,
        candidate_type=candidate_type,
        evidence={
            "candidate_git_sha": target,
            "candidate_type": candidate_type,
            "job_human_id": job.human_id,
        },
    )


def _emit_work_execution_unavailable(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    work: RemediationWorkRecord,
) -> RemediationExecutionResult:
    evidence = {
        "work_item_id": work.work_item_id,
        "reason": "WORK_EXECUTION_UNAVAILABLE",
        "message": "Remediation requires ServiceContext or an explicit test worker",
    }
    update_remediation_work(conn, work.work_item_id, status="FAILED", result=evidence)
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="WORK_EXECUTION_UNAVAILABLE",
        summary=f"Remediation work {work.work_item_id} cannot execute without worker context.",
        actor_id=work.assigned_agent,
        phase="REMEDIATION",
        evidence=evidence,
    )
    return RemediationExecutionResult(
        work_item_id=work.work_item_id,
        status="UNAVAILABLE",
        target_candidate_id=None,
        evidence=evidence,
    )


def execute_remediation_work(
    conn: sqlite3.Connection,
    *,
    work: RemediationWorkRecord,
    event_ctx: EventContext,
    project_id: str,
    repository_root: str,
    worker: RemediationWorker | None = None,
    service_ctx=None,
) -> RemediationExecutionResult:
    """Run remediation work; production requires ServiceContext or explicit worker injection."""
    from projectos.event_truthfulness import require_persisted_work

    require_persisted_work(conn, work_item_id=work.work_item_id, orchestration_job_id=work.orchestration_job_id)

    if worker is None and service_ctx is None:
        return _emit_work_execution_unavailable(conn, event_ctx=event_ctx, work=work)

    update_remediation_work(conn, work.work_item_id, status="RUNNING")
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="WORK_STARTED",
        summary=f"Remediation work {work.work_item_id} started.",
        actor_id=work.assigned_agent,
        phase="REMEDIATION",
        evidence={
            "work_item_id": work.work_item_id,
            "orchestration_job_id": work.orchestration_job_id,
            "source_candidate_id": work.source_candidate_id,
            "remediation_cycle": work.remediation_cycle,
        },
    )

    runner = worker
    if runner is None:
        runner = lambda c, **kwargs: production_remediation_worker(
            c, service_ctx=service_ctx, **kwargs
        )

    try:
        outcome = runner(
            conn,
            work=work,
            event_ctx=event_ctx,
            project_id=project_id,
            repository_root=repository_root,
        )
    except Exception as exc:
        update_remediation_work(
            conn,
            work.work_item_id,
            status="FAILED",
            result={"error": str(exc)},
        )
        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="WORK_FAILED",
            summary=f"Remediation work {work.work_item_id} failed.",
            actor_id=work.assigned_agent,
            phase="REMEDIATION",
            evidence={"work_item_id": work.work_item_id, "error": str(exc)[:500]},
        )
        raise

    if outcome.status == "UNAVAILABLE":
        return outcome

    if outcome.status != "COMPLETED" or not outcome.target_candidate_id:
        update_remediation_work(
            conn,
            work.work_item_id,
            status="FAILED",
            result=outcome.evidence,
        )
        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="WORK_FAILED",
            summary=f"Remediation work {work.work_item_id} failed.",
            actor_id=work.assigned_agent,
            phase="REMEDIATION",
            evidence=outcome.evidence or {"reason": "missing_candidate"},
        )
        return RemediationExecutionResult(
            work_item_id=work.work_item_id,
            status="FAILED",
            target_candidate_id=None,
            evidence=outcome.evidence,
        )

    try:
        validate_candidate_identity(
            outcome.target_candidate_id,
            candidate_type=outcome.candidate_type,
            repository_root=repository_root,
        )
    except OrchestrationError as exc:
        update_remediation_work(
            conn,
            work.work_item_id,
            status="FAILED",
            result={"reason": "invalid_candidate", "message": str(exc)},
        )
        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="WORK_FAILED",
            summary=f"Remediation candidate rejected: {exc}",
            actor_id=work.assigned_agent,
            phase="REMEDIATION",
            evidence={"work_item_id": work.work_item_id, "error": str(exc)},
        )
        return RemediationExecutionResult(
            work_item_id=work.work_item_id,
            status="FAILED",
            target_candidate_id=None,
            evidence={"reason": "invalid_candidate", "message": str(exc)},
        )

    set_run_active_candidate(
        conn,
        run_id=work.run_id,
        candidate_id=outcome.target_candidate_id,
        candidate_type=outcome.candidate_type,
        remediation_cycle=work.remediation_cycle,
    )
    update_remediation_work(
        conn,
        work.work_item_id,
        status="COMPLETED",
        target_candidate_id=outcome.target_candidate_id,
        result=outcome.evidence,
    )
    completion_evidence = {
        "work_item_id": work.work_item_id,
        "target_candidate_id": outcome.target_candidate_id,
        "candidate_type": outcome.candidate_type,
        **outcome.evidence,
    }
    from projectos.event_truthfulness import require_work_completion_evidence

    require_work_completion_evidence(completion_evidence)
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="WORK_COMPLETED",
        summary=f"Remediation completed; candidate {outcome.target_candidate_id}.",
        actor_id=work.assigned_agent,
        phase="REMEDIATION",
        evidence=completion_evidence,
    )
    return outcome
