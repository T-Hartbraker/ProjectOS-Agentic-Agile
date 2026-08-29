"""Execute remediation work through the authoritative worker backbone."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from projectos.candidate_model import next_remediation_candidate_sha, set_run_active_candidate
from projectos.constants import ASSURANCE_QUEUES, QUEUE_TO_ROLE
from projectos.domain_events import EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.remediation_store import RemediationWorkRecord, update_remediation_work
from projectos.store import create_job, get_job, insert_qa_evidence, mark_succeeded


@dataclass(frozen=True)
class RemediationExecutionResult:
    work_item_id: str
    status: str
    target_candidate_id: str | None
    evidence: dict[str, Any]


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


def _create_retest_evidence_for_candidate(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    repository_root: str,
    run_id: str,
    candidate_sha: str,
    remediation_cycle: int,
    source_roles: list[str],
    result: str = "pending",
) -> list[int]:
    """Append new QA evidence rows for a new candidate (never mutate old rows)."""
    delivery_job = conn.execute(
        """
        SELECT id FROM orchestration_jobs
        WHERE project_human_id = ? AND human_id LIKE ?
        ORDER BY id DESC LIMIT 1
        """,
        (project_id, f"{run_id}__REMEDIATION_%"),
    ).fetchone()
    delivery_job_id = int(delivery_job["id"]) if delivery_job else 0
    if delivery_job_id == 0:
        delivery = create_job(
            conn,
            human_id=f"{run_id}__REMEDIATION_DELIVERY",
            project_human_id=project_id,
            repository_root=repository_root,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
        )
        delivery_job_id = delivery.id

    evidence_ids: list[int] = []
    normalized_roles: list[str] = []
    for role in source_roles or list(ASSURANCE_QUEUES):
        if role in ASSURANCE_QUEUES:
            normalized_roles.append(role)
        else:
            normalized_roles.append("ASSURANCE_FUNCTIONAL")
    if not normalized_roles:
        normalized_roles = list(ASSURANCE_QUEUES)
    seen: set[str] = set()
    for queue in normalized_roles:
        if queue in seen:
            continue
        seen.add(queue)
        agent_role = QUEUE_TO_ROLE[queue]
        assurance = create_job(
            conn,
            human_id=f"{run_id}__QA_{candidate_sha[:12]}__{queue}__c{remediation_cycle}",
            project_human_id=project_id,
            repository_root=repository_root,
            agent_role=agent_role,
            queue=queue,
            status="SUCCEEDED",
            base_git_sha=candidate_sha,
        )
        assurance_job_id = assurance.id
        cur = conn.execute(
            """
            INSERT INTO qa_evidence (
                project_human_id, repository_root, delivery_job_id, assurance_job_id,
                candidate_git_sha, assurance_role, result, run_id, remediation_cycle
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                repository_root,
                delivery_job_id,
                assurance_job_id,
                candidate_sha,
                queue,
                result,
                run_id,
                remediation_cycle,
            ),
        )
        evidence_ids.append(int(cur.lastrowid))
    return evidence_ids


def default_remediation_worker(
    conn: sqlite3.Connection,
    *,
    work: RemediationWorkRecord,
    event_ctx: EventContext,
    project_id: str,
    repository_root: str,
    service_ctx=None,
) -> RemediationExecutionResult:
    """Invoke orchestration job via WorkerService when available."""
    if work.orchestration_job_id is None:
        raise OrchestrationError("Remediation work missing orchestration job")
    job = get_job(conn, work.orchestration_job_id)
    if job is None:
        raise OrchestrationError(f"Remediation job id={work.orchestration_job_id} not found")
    if service_ctx is not None:
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
    else:
        target = next_remediation_candidate_sha(
            work.source_candidate_id or "sha0000", work.remediation_cycle
        )
        mark_succeeded(
            conn,
            job.id,
            output_ref="pm-remediation",
            candidate_git_sha=target,
        )
    if not target:
        target = next_remediation_candidate_sha(work.source_candidate_id or "sha0000", work.remediation_cycle)
    return RemediationExecutionResult(
        work_item_id=work.work_item_id,
        status="COMPLETED",
        target_candidate_id=target,
        evidence={"candidate_git_sha": target, "job_human_id": job.human_id},
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
    retest_roles: list[str] | None = None,
    retest_result: str = "pending",
) -> RemediationExecutionResult:
    """Run remediation work and produce a new candidate with fresh QA evidence."""
    from projectos.event_truthfulness import require_persisted_work

    require_persisted_work(conn, work_item_id=work.work_item_id, orchestration_job_id=work.orchestration_job_id)

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
        runner = lambda c, **kwargs: default_remediation_worker(
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
            evidence=outcome.evidence,
        )
        return outcome

    roles = retest_roles or []
    if not roles:
        roles = list(ASSURANCE_QUEUES)
    _create_retest_evidence_for_candidate(
        conn,
        project_id=project_id,
        repository_root=repository_root,
        run_id=work.run_id,
        candidate_sha=outcome.target_candidate_id,
        remediation_cycle=work.remediation_cycle,
        source_roles=roles,
        result=retest_result,
    )
    set_run_active_candidate(
        conn,
        run_id=work.run_id,
        candidate_id=outcome.target_candidate_id,
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
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="QA_RETEST_STARTED",
        summary=f"QA retesting candidate {outcome.target_candidate_id}.",
        actor_id="qa-agent",
        phase="QA_GATE",
        evidence={
            "candidate_id": outcome.target_candidate_id,
            "candidate_type": "git_sha",
            "remediation_cycle": work.remediation_cycle,
            "source_candidate_id": work.source_candidate_id,
        },
    )
    return outcome
