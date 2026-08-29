"""Execute real QA assurance retests against remediation candidates."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from projectos.candidate_model import (
    CANDIDATE_TYPE_GIT_SHA,
    validate_candidate_identity,
)
from projectos.constants import ASSURANCE_QUEUES
from projectos.domain_events import ACTOR_QA, EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.qa_gate import collect_qa_gate_facts
from projectos.qa_handoff import create_assurance_jobs_for_producer
from projectos.store import get_job, get_job_by_human_id


class AssuranceExecutor(Protocol):
    def __call__(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        repository_root: str,
        candidate_id: str,
        candidate_type: str,
        run_id: str | None,
        remediation_cycle: int,
        assurance_job_ids: list[int],
    ) -> None: ...


@dataclass(frozen=True)
class QARetestResult:
    gate: str
    candidate_id: str
    candidate_type: str
    assurance_job_ids: tuple[int, ...] = ()
    unavailable: bool = False


def _resolve_producer_job(
    conn: sqlite3.Connection,
    *,
    source_remediation_job_id: int | None,
    candidate_id: str,
) -> Any:
    if source_remediation_job_id is None:
        raise OrchestrationError("QA retest requires producer remediation job id")
    producer = get_job(conn, source_remediation_job_id)
    if producer is None:
        raise OrchestrationError(f"Producer job id={source_remediation_job_id} not found")
    if producer.status != "SUCCEEDED":
        raise OrchestrationError(
            f"Producer job {producer.human_id} must be SUCCEEDED before QA retest"
        )
    if str(producer.candidate_git_sha or "") != candidate_id:
        raise OrchestrationError(
            f"Producer candidate {producer.candidate_git_sha!r} != retest candidate {candidate_id!r}"
        )
    return producer


def _tag_evidence_run(
    conn: sqlite3.Connection,
    *,
    assurance_job_ids: list[int],
    candidate_id: str,
    run_id: str | None,
    remediation_cycle: int,
) -> None:
    if not assurance_job_ids:
        return
    placeholders = ",".join("?" * len(assurance_job_ids))
    conn.execute(
        f"""
        UPDATE qa_evidence
        SET run_id = ?, remediation_cycle = ?
        WHERE assurance_job_id IN ({placeholders})
          AND candidate_git_sha = ?
        """,
        (run_id, remediation_cycle, *assurance_job_ids, candidate_id),
    )


def _partition_assurance_jobs(
    conn: sqlite3.Connection,
    assurance_job_ids: list[int],
    *,
    retest_roles: list[str] | None = None,
) -> tuple[list[int], int | None]:
    assessor_ids: list[int] = []
    manager_id: int | None = None
    allowed = {r.upper() for r in retest_roles} if retest_roles else None
    for job_id in assurance_job_ids:
        job = get_job(conn, job_id)
        if job is None:
            continue
        if job.queue == "QA_MANAGER":
            manager_id = job_id
        elif job.queue in ASSURANCE_QUEUES:
            if allowed is None or job.queue.upper() in allowed:
                assessor_ids.append(job_id)
    return assessor_ids, manager_id


def _run_assurance_via_worker(
    conn: sqlite3.Connection,
    *,
    service_ctx,
    assurance_job_ids: list[int],
    retest_roles: list[str] | None = None,
) -> None:
    from projectos.services.facades import WorkerService
    from projectos.worker_status import worker_succeeded

    assessor_ids, manager_id = _partition_assurance_jobs(
        conn, assurance_job_ids, retest_roles=retest_roles
    )
    worker = WorkerService(service_ctx)
    for job_id in assessor_ids:
        job = get_job(conn, job_id)
        if job is None:
            raise OrchestrationError(f"Assurance job id={job_id} not found")
        outcome = worker.run_once(job_human_id=job.human_id)
        if not worker_succeeded(outcome.status):
            raise OrchestrationError(
                f"Assurance worker {job.human_id} execution failed: {outcome.message}"
            )
    if manager_id is not None:
        job = get_job(conn, manager_id)
        if job is None:
            raise OrchestrationError(f"QA Manager job id={manager_id} not found")
        outcome = worker.run_once(job_human_id=job.human_id)
        if not worker_succeeded(outcome.status):
            raise OrchestrationError(
                f"QA Manager worker {job.human_id} execution failed: {outcome.message}"
            )


def execute_qa_retest(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    project_id: str,
    repository_root: str,
    candidate_id: str,
    candidate_type: str = CANDIDATE_TYPE_GIT_SHA,
    run_id: str | None,
    remediation_cycle: int,
    retest_roles: list[str] | None = None,
    source_remediation_job_id: int | None = None,
    source_candidate_id: str | None = None,
    service_ctx=None,
    assurance_executor: AssuranceExecutor | None = None,
) -> QARetestResult:
    """Run authoritative assurance jobs for a remediation candidate."""
    from projectos.orchestration_boundary import run_with_internal_defect_routing

    return run_with_internal_defect_routing(
        conn,
        event_ctx=event_ctx,
        project_id=project_id,
        component="qa_retest",
        operation="execute_qa_retest",
        in_project_scope=True,
        service_ctx=service_ctx,
        repository_root=repository_root,
        fn=lambda: _execute_qa_retest_impl(
            conn,
            event_ctx=event_ctx,
            project_id=project_id,
            repository_root=repository_root,
            candidate_id=candidate_id,
            candidate_type=candidate_type,
            run_id=run_id,
            remediation_cycle=remediation_cycle,
            retest_roles=retest_roles,
            source_remediation_job_id=source_remediation_job_id,
            source_candidate_id=source_candidate_id,
            service_ctx=service_ctx,
            assurance_executor=assurance_executor,
        ),
    )


def _execute_qa_retest_impl(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    project_id: str,
    repository_root: str,
    candidate_id: str,
    candidate_type: str = CANDIDATE_TYPE_GIT_SHA,
    run_id: str | None,
    remediation_cycle: int,
    retest_roles: list[str] | None = None,
    source_remediation_job_id: int | None = None,
    source_candidate_id: str | None = None,
    service_ctx=None,
    assurance_executor: AssuranceExecutor | None = None,
) -> QARetestResult:
    """Internal QA retest execution — wrapped by orchestration boundary."""
    validate_candidate_identity(
        candidate_id,
        candidate_type=candidate_type,
        repository_root=repository_root,
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="QA_RETEST_STARTED",
        summary=f"QA retesting candidate {candidate_id}.",
        actor_id=ACTOR_QA,
        phase="QA_GATE",
        evidence={
            "candidate_id": candidate_id,
            "candidate_type": candidate_type,
            "remediation_cycle": remediation_cycle,
            "run_id": run_id,
        },
    )

    producer = _resolve_producer_job(
        conn,
        source_remediation_job_id=source_remediation_job_id,
        candidate_id=candidate_id,
    )
    new_candidate = bool(source_candidate_id and source_candidate_id != candidate_id)
    effective_retest_roles = None if new_candidate else retest_roles

    handoff = create_assurance_jobs_for_producer(
        conn,
        producer,
        candidate_git_sha=candidate_id,
    )
    assurance_ids = []
    for human_id in handoff.assurance_job_ids:
        job = get_job_by_human_id(conn, human_id)
        if job is not None:
            assurance_ids.append(job.id)
    _tag_evidence_run(
        conn,
        assurance_job_ids=assurance_ids,
        candidate_id=candidate_id,
        run_id=run_id,
        remediation_cycle=remediation_cycle,
    )

    if assurance_executor is not None:
        assurance_executor(
            conn,
            project_id=project_id,
            repository_root=repository_root,
            candidate_id=candidate_id,
            candidate_type=candidate_type,
            run_id=run_id,
            remediation_cycle=remediation_cycle,
            assurance_job_ids=assurance_ids,
        )
    elif service_ctx is not None:
        _run_assurance_via_worker(
            conn,
            service_ctx=service_ctx,
            assurance_job_ids=assurance_ids,
            retest_roles=effective_retest_roles,
        )
    else:
        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="QA_EXECUTION_UNAVAILABLE",
            summary="QA retest cannot run without assurance executor context.",
            actor_id=ACTOR_QA,
            phase="QA_GATE",
            evidence={"candidate_id": candidate_id, "reason": "missing_service_ctx"},
        )
        return QARetestResult(
            gate="PENDING",
            candidate_id=candidate_id,
            candidate_type=candidate_type,
            assurance_job_ids=tuple(assurance_ids),
            unavailable=True,
        )

    facts = collect_qa_gate_facts(
        conn,
        project_id=project_id,
        candidate_git_sha=candidate_id,
        run_id=run_id,
    )
    gate = str(facts.get("gate") or "PENDING")
    return QARetestResult(
        gate=gate,
        candidate_id=candidate_id,
        candidate_type=candidate_type,
        assurance_job_ids=tuple(assurance_ids),
    )
