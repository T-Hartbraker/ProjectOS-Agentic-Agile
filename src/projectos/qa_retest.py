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
from projectos.qa_handoff import create_assurance_jobs_for_delivery, record_assurance_result
from projectos.store import create_job, get_job, get_job_by_human_id, mark_succeeded


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


def _ensure_remediation_delivery_job(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    repository_root: str,
    run_id: str,
    remediation_cycle: int,
    candidate_id: str,
    source_job_id: int | None,
    source_candidate_id: str | None = None,
) -> int:
    base_sha = source_candidate_id
    if source_job_id is not None:
        source = get_job(conn, source_job_id)
        if source is not None:
            base_sha = source.base_git_sha or source.candidate_git_sha or base_sha
            if source.candidate_git_sha == candidate_id:
                if source.status != "SUCCEEDED":
                    mark_succeeded(
                        conn,
                        source.id,
                        output_ref="remediation-complete",
                        candidate_git_sha=candidate_id,
                    )
                return source.id
    row = conn.execute(
        """
        SELECT id FROM orchestration_jobs
        WHERE project_human_id = ? AND human_id = ?
        """,
        (project_id, f"{run_id}__REMEDIATION_DELIVERY__c{remediation_cycle}"),
    ).fetchone()
    if row:
        delivery_id = int(row["id"])
        mark_succeeded(
            conn,
            delivery_id,
            output_ref="remediation-complete",
            candidate_git_sha=candidate_id,
        )
        return delivery_id
    delivery = create_job(
        conn,
        human_id=f"{run_id}__REMEDIATION_DELIVERY__c{remediation_cycle}",
        project_human_id=project_id,
        repository_root=repository_root,
        agent_role="DELIVERY",
        queue="DELIVERY",
        status="SUCCEEDED",
        base_git_sha=base_sha or candidate_id,
        candidate_git_sha=candidate_id,
    )
    return delivery.id


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


def _run_assurance_via_worker(
    conn: sqlite3.Connection,
    *,
    service_ctx,
    assurance_job_ids: list[int],
) -> None:
    from projectos.services.facades import WorkerService

    worker = WorkerService(service_ctx)
    for job_id in assurance_job_ids:
        job = get_job(conn, job_id)
        if job is None:
            raise OrchestrationError(f"Assurance job id={job_id} not found")
        outcome = worker.run_once(job_human_id=job.human_id)
        if outcome.status != "SUCCEEDED":
            assurance = get_job(conn, job_id)
            if assurance is not None:
                record_assurance_result(
                    conn,
                    assurance,
                    passed=False,
                    evidence_ref=outcome.message,
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

    delivery_id = _ensure_remediation_delivery_job(
        conn,
        project_id=project_id,
        repository_root=repository_root,
        run_id=run_id or project_id,
        remediation_cycle=remediation_cycle,
        candidate_id=candidate_id,
        source_job_id=source_remediation_job_id,
        source_candidate_id=source_candidate_id,
    )
    delivery = get_job(conn, delivery_id)
    if delivery is None:
        raise OrchestrationError(f"Remediation delivery job id={delivery_id} missing")

    handoff = create_assurance_jobs_for_delivery(
        conn,
        delivery,
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
        _run_assurance_via_worker(conn, service_ctx=service_ctx, assurance_job_ids=assurance_ids)
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
