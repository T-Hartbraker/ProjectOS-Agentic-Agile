"""Delivery → independent QA handoff and stale-evidence rejection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from projectos.assurance_verdict import (
    VERDICT_FAIL,
    VERDICT_INCONCLUSIVE,
    VERDICT_PASS,
    AssuranceResult,
    AssuranceValidationError,
    parse_and_validate_assurance_result,
    verdict_to_evidence_result,
)
from projectos.constants import ASSURANCE_QUEUES, CODE_MODIFYING_ROLES, QUEUE_TO_ROLE
from projectos.delivery_evidence import is_valid_qa_candidate
from projectos.errors import OrchestrationError
from projectos.projectctl_bridge import create_defect
from projectos.store import (
    OrchestrationJob,
    add_job_dependency,
    append_run_event,
    create_job,
    get_job,
    insert_qa_evidence,
    set_job_source_provenance,
)

REQUIRED_ASSURANCE = (
    "ASSURANCE_FUNCTIONAL",
    "ASSURANCE_INTEGRATION",
    "ASSURANCE_SECURITY",
    "ASSURANCE_QUALITY",
)


@dataclass(frozen=True)
class HandoffResult:
    delivery_job_id: int
    candidate_git_sha: str
    assurance_job_ids: list[str]


def create_assurance_jobs_for_delivery(
    conn,
    delivery: OrchestrationJob,
    *,
    candidate_git_sha: str,
) -> HandoffResult:
    if not candidate_git_sha:
        raise OrchestrationError("DELIVERY success requires candidate_git_sha")
    if delivery.base_git_sha and candidate_git_sha == delivery.base_git_sha:
        raise OrchestrationError(
            "QA handoff refused: candidate_git_sha equals base_git_sha (no-op)"
        )
    if delivery.outcome in {"INVALIDATED", "SUPERSEDED", "NO_CHANGE"}:
        raise OrchestrationError(
            f"QA handoff refused: delivery outcome={delivery.outcome}"
        )
    identity = None
    if delivery.identity_snapshot_json:
        try:
            identity = json.loads(delivery.identity_snapshot_json)
        except json.JSONDecodeError:
            identity = None
    if identity is None:
        identity = {
            "project_human_id": delivery.project_human_id,
            "repository_root": delivery.repository_root,
        }

    created: list[str] = []
    for queue in REQUIRED_ASSURANCE:
        human_id = f"{delivery.human_id}__{queue}"
        job = create_job(
            conn,
            human_id=human_id,
            project_human_id=delivery.project_human_id,
            repository_root=delivery.repository_root,
            agent_role=QUEUE_TO_ROLE[queue],
            queue=queue,
            status="READY",
            iteration_human_id=delivery.iteration_human_id,
            work_item_type=delivery.work_item_type,
            work_item_human_id=delivery.work_item_human_id,
            requires_worktree=True,
            worktree_name=f"{delivery.project_human_id}__{human_id}",
            base_git_sha=candidate_git_sha,
            identity_snapshot=identity,
            run_id=getattr(delivery, "run_id", None),
        )
        set_job_source_provenance(
            conn,
            job.id,
            source_delivery_job_id=delivery.id,
            source_candidate_sha=candidate_git_sha,
        )
        add_job_dependency(conn, job.id, delivery.id)
        insert_qa_evidence(
            conn,
            project_human_id=delivery.project_human_id,
            repository_root=delivery.repository_root,
            delivery_job_id=delivery.id,
            assurance_job_id=job.id,
            candidate_git_sha=candidate_git_sha,
            assurance_role=queue,
            result="pending",
        )
        created.append(human_id)

    # QA Manager aggregation job waits on all assurance jobs.
    from projectos.qa_manager import QA_MANAGER_ROLE

    agg_id = f"{delivery.human_id}__QA_MANAGER"
    agg = create_job(
        conn,
        human_id=agg_id,
        project_human_id=delivery.project_human_id,
        repository_root=delivery.repository_root,
        agent_role=QA_MANAGER_ROLE,
        queue=QA_MANAGER_ROLE,
        status="READY",
        iteration_human_id=delivery.iteration_human_id,
        requires_worktree=False,
        identity_snapshot=identity,
    )
    set_job_source_provenance(
        conn,
        agg.id,
        source_delivery_job_id=delivery.id,
        source_candidate_sha=candidate_git_sha,
    )
    for queue in REQUIRED_ASSURANCE:
        row = conn.execute(
            "SELECT id FROM orchestration_jobs WHERE human_id = ?",
            (f"{delivery.human_id}__{queue}",),
        ).fetchone()
        if row:
            add_job_dependency(conn, agg.id, int(row[0]))
    insert_qa_evidence(
        conn,
        project_human_id=delivery.project_human_id,
        repository_root=delivery.repository_root,
        delivery_job_id=delivery.id,
        assurance_job_id=agg.id,
        candidate_git_sha=candidate_git_sha,
        assurance_role=QA_MANAGER_ROLE,
        result="pending",
    )
    created.append(agg_id)

    append_run_event(
        conn,
        delivery.id,
        "qa.handoff_created",
        status="SUCCEEDED",
        message=f"Created assurance jobs for candidate {candidate_git_sha}",
        payload={"assurance_jobs": created, "candidate_git_sha": candidate_git_sha},
    )
    return HandoffResult(
        delivery_job_id=delivery.id,
        candidate_git_sha=candidate_git_sha,
        assurance_job_ids=created,
    )


def create_assurance_jobs_for_producer(
    conn,
    producer: OrchestrationJob,
    *,
    candidate_git_sha: str,
) -> HandoffResult:
    """Create assurance jobs for a real succeeded producer job (delivery or remediation)."""
    if producer.status != "SUCCEEDED":
        raise OrchestrationError(
            f"QA handoff refused: producer job {producer.human_id} is not SUCCEEDED"
        )
    if not producer.candidate_git_sha or producer.candidate_git_sha != candidate_git_sha:
        raise OrchestrationError(
            "QA handoff refused: producer candidate does not match requested candidate"
        )
    return create_assurance_jobs_for_delivery(conn, producer, candidate_git_sha=candidate_git_sha)


def _next_assurance_retry_attempt(
    conn,
    *,
    run_id: str,
    candidate_git_sha: str,
    assessor_queue: str,
) -> int:
    pattern = f"{run_id}__{candidate_git_sha[:12]}__{assessor_queue}__RETRY_%"
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM orchestration_jobs
        WHERE human_id LIKE ? AND queue = ?
        """,
        (pattern, assessor_queue),
    ).fetchone()
    return int(row["c"]) + 1 if row else 1


def create_assurance_retry(
    conn,
    *,
    run_id: str,
    project_id: str,
    repository_root: str,
    candidate_git_sha: str,
    assessor_queue: str,
    producer_job_id: int,
    prior_assurance_job_id: int | None = None,
    attempt_number: int | None = None,
) -> OrchestrationJob:
    """Create a real assurance retry job with candidate/evidence lineage."""
    producer = get_job(conn, producer_job_id)
    if producer is None:
        raise OrchestrationError(f"Producer job {producer_job_id} not found")
    attempt = attempt_number or _next_assurance_retry_attempt(
        conn,
        run_id=run_id,
        candidate_git_sha=candidate_git_sha,
        assessor_queue=assessor_queue,
    )
    human_id = (
        f"{run_id}__{candidate_git_sha[:12]}__{assessor_queue}__RETRY_{attempt:03d}"
    )
    existing = conn.execute(
        "SELECT id FROM orchestration_jobs WHERE human_id = ?",
        (human_id,),
    ).fetchone()
    if existing is not None:
        return get_job(conn, int(existing["id"]))
    identity = None
    if producer.identity_snapshot_json:
        try:
            identity = json.loads(producer.identity_snapshot_json)
        except json.JSONDecodeError:
            identity = None
    if identity is None:
        identity = {
            "project_human_id": project_id,
            "repository_root": repository_root,
        }
    job = create_job(
        conn,
        human_id=human_id,
        project_human_id=project_id,
        repository_root=repository_root,
        agent_role=QUEUE_TO_ROLE[assessor_queue],
        queue=assessor_queue,
        status="READY",
        requires_worktree=True,
        worktree_name=f"{project_id}__{human_id}",
        base_git_sha=candidate_git_sha,
        identity_snapshot=identity,
        run_id=run_id,
    )
    set_job_source_provenance(
        conn,
        job.id,
        source_delivery_job_id=producer_job_id,
        source_candidate_sha=candidate_git_sha,
    )
    if prior_assurance_job_id is not None:
        add_job_dependency(conn, job.id, prior_assurance_job_id)
    insert_qa_evidence(
        conn,
        project_human_id=project_id,
        repository_root=repository_root,
        delivery_job_id=producer_job_id,
        assurance_job_id=job.id,
        candidate_git_sha=candidate_git_sha,
        assurance_role=assessor_queue,
        result="pending",
        run_id=run_id,
        attempt_number=attempt,
        prior_assurance_job_id=prior_assurance_job_id,
    )
    return job


def _assert_assurance_recording_allowed(assurance: OrchestrationJob) -> str:
    if (
        assurance.queue not in ASSURANCE_QUEUES
        or assurance.agent_role in CODE_MODIFYING_ROLES
    ):
        raise OrchestrationError(
            "Developer runs cannot record QA results; independent assurance only"
        )
    expected = assurance.source_candidate_sha
    if not expected:
        raise OrchestrationError("Assurance job missing source_candidate_sha")
    return expected


def _reject_stale_assurance_evidence(
    conn,
    assurance: OrchestrationJob,
    *,
    evidence_ref: str | None = None,
) -> None:
    expected = _assert_assurance_recording_allowed(assurance)
    if assurance.source_delivery_job_id is not None:
        delivery = get_job(conn, assurance.source_delivery_job_id)
        current = delivery.candidate_git_sha
        if current and current != expected:
            from projectos.qa_evidence_policy import update_qa_evidence_result

            update_qa_evidence_result(
                conn,
                assurance_job_id=assurance.id,
                candidate_git_sha=expected,
                new_result="stale_rejected",
                evidence_ref=evidence_ref,
            )
            append_run_event(
                conn,
                assurance.id,
                "qa.stale_evidence_rejected",
                status="BLOCKED",
                message=(
                    f"QA evidence for {expected} cannot approve newer "
                    f"candidate {current}"
                ),
            )
            raise OrchestrationError(
                f"Stale QA evidence for {expected}; delivery candidate is {current}"
            )


def record_invalid_assurance_result(
    conn,
    assurance: OrchestrationJob,
    *,
    reason: str,
    evidence_ref: str | None = None,
) -> str:
    """Record malformed/missing structured verdict — never PASS."""
    expected = _assert_assurance_recording_allowed(assurance)
    try:
        _reject_stale_assurance_evidence(conn, assurance, evidence_ref=evidence_ref)
    except OrchestrationError:
        raise

    from projectos.domain_events import lookup_event_context_for_job
    from projectos.qa_evidence_policy import update_qa_evidence_result
    from projectos.qa_gate import emit_qa_gate_evaluation

    update_qa_evidence_result(
        conn,
        assurance_job_id=assurance.id,
        candidate_git_sha=expected,
        new_result="inconclusive",
        evidence_ref=evidence_ref,
    )
    append_run_event(
        conn,
        assurance.id,
        "qa.assurance_result_invalid",
        status="BLOCKED",
        message=reason[:500],
        payload={"reason": reason, "candidate_git_sha": expected},
    )
    event_ctx = lookup_event_context_for_job(conn, assurance.id)
    if event_ctx is not None:
        from projectos.domain_events import ACTOR_QA, emit_projectos_event

        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="ASSURANCE_RESULT_INVALID",
            summary=f"Assurance result invalid for {assurance.queue}: {reason[:200]}",
            actor_id=ACTOR_QA,
            phase="QA_GATE",
            detail_level="milestone",
            evidence={
                "assurance_role": assurance.queue,
                "job_id": assurance.human_id,
                "candidate_git_sha": expected,
                "reason": reason,
            },
        )
        emit_qa_gate_evaluation(
            conn,
            project_id=assurance.project_human_id,
            event_context=event_ctx,
        )
    return "inconclusive"


def record_assurance_verdict(
    conn,
    assurance: OrchestrationJob,
    *,
    result: AssuranceResult,
    evidence_ref: str | None = None,
    create_defect_fn=None,
) -> str:
    """Record validated assurance verdict — execution success is not implied."""
    expected = _assert_assurance_recording_allowed(assurance)
    _reject_stale_assurance_evidence(conn, assurance, evidence_ref=evidence_ref)

    if result.candidate_id != expected:
        raise OrchestrationError(
            f"Candidate mismatch: result {result.candidate_id!r} != job {expected!r}"
        )
    if result.assurance_job_id != assurance.human_id:
        raise OrchestrationError(
            f"Job mismatch: result {result.assurance_job_id!r} != job {assurance.human_id!r}"
        )
    if result.assessor_role != assurance.queue:
        raise OrchestrationError(
            f"Role mismatch: result {result.assessor_role!r} != job {assurance.queue!r}"
        )

    evidence_result = verdict_to_evidence_result(result.verdict)
    from projectos.qa_evidence_policy import update_qa_evidence_result

    update_qa_evidence_result(
        conn,
        assurance_job_id=assurance.id,
        candidate_git_sha=expected,
        new_result=evidence_result,
        evidence_ref=evidence_ref,
    )
    append_run_event(
        conn,
        assurance.id,
        "qa.assurance_result_recorded",
        status="SUCCEEDED",
        message=f"Assurance verdict {result.verdict} for {expected}",
        payload={
            "verdict": result.verdict,
            "candidate_git_sha": expected,
            "summary": result.summary,
            "findings_count": len(result.findings),
        },
    )

    from projectos.domain_events import lookup_event_context_for_job
    from projectos.qa_gate import emit_qa_finding_created, emit_qa_gate_evaluation

    event_ctx = lookup_event_context_for_job(conn, assurance.id)
    if event_ctx is not None:
        from projectos.domain_events import ACTOR_QA, emit_projectos_event

        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="ASSURANCE_RESULT_RECORDED",
            summary=f"{assurance.queue} verdict {result.verdict} for {expected}",
            actor_id=ACTOR_QA,
            phase="QA_GATE",
            detail_level="normal",
            evidence={
                "verdict": result.verdict,
                "assurance_role": assurance.queue,
                "job_id": assurance.human_id,
                "candidate_git_sha": expected,
                "summary": result.summary,
            },
        )
        if result.verdict == VERDICT_FAIL:
            for finding in result.findings:
                emit_qa_finding_created(
                    conn,
                    event_context=event_ctx,
                    summary=f"QA finding {finding.finding_id}: {finding.actual_condition}",
                    evidence=finding.to_dict(),
                )
            if not result.findings:
                emit_qa_finding_created(
                    conn,
                    event_context=event_ctx,
                    summary=f"QA finding on {assurance.queue} for {expected}",
                    evidence={
                        "assurance_role": assurance.queue,
                        "job_id": assurance.human_id,
                        "candidate_git_sha": expected,
                        "result": evidence_result,
                        "summary": result.summary,
                    },
                )
        elif result.verdict == VERDICT_INCONCLUSIVE:
            emit_projectos_event(
                conn,
                ctx=event_ctx,
                event_type="QA_INCONCLUSIVE",
                summary=(
                    f"Assurance {assurance.queue} inconclusive for {expected}: "
                    f"{result.summary or 'assessment could not determine compliance'}"
                ),
                actor_id=ACTOR_QA,
                phase="QA_GATE",
                detail_level="milestone",
                evidence={
                    "assurance_role": assurance.queue,
                    "job_id": assurance.human_id,
                    "candidate_git_sha": expected,
                    "summary": result.summary,
                },
            )
        emit_qa_gate_evaluation(
            conn,
            project_id=assurance.project_human_id,
            event_context=event_ctx,
        )

    if result.verdict == VERDICT_FAIL:
        defect_id = None
        if create_defect_fn is not None:
            defect_out = create_defect_fn(
                Path(assurance.repository_root),
                title=f"QA failure on {assurance.queue} for {expected}",
                description=f"Assurance job {assurance.human_id} failed: {result.summary}",
            )
            for line in (defect_out.stdout or "").splitlines():
                if "Created" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        defect_id = parts[1]
                        break
        if defect_id:
            conn.execute(
                """
                UPDATE qa_evidence SET defect_human_id = ?
                WHERE assurance_job_id = ?
                """,
                (defect_id, assurance.id),
            )
        append_run_event(
            conn,
            assurance.id,
            "qa.failed",
            status="FAILED",
            message="QA failure recorded; PM owns corrective work",
            payload={"defect": defect_id, "candidate_git_sha": expected},
        )
    return evidence_result


def process_assurance_worker_success(
    conn,
    assurance: OrchestrationJob,
    *,
    stdout: str | None,
    evidence_ref: str | None = None,
    create_defect_fn=None,
) -> str:
    """Parse structured worker output after successful execution — never auto-PASS."""
    try:
        validated = parse_and_validate_assurance_result(stdout, assurance)
    except AssuranceValidationError as exc:
        return record_invalid_assurance_result(
            conn,
            assurance,
            reason=str(exc),
            evidence_ref=evidence_ref,
        )
    return record_assurance_verdict(
        conn,
        assurance,
        result=validated,
        evidence_ref=evidence_ref,
        create_defect_fn=create_defect_fn,
    )


def record_assurance_result(
    conn,
    assurance: OrchestrationJob,
    *,
    passed: bool | None = None,
    verdict: str | None = None,
    evidence_ref: str | None = None,
    create_defect_fn=None,
    summary: str = "",
    findings: list | None = None,
) -> None:
    """Backward-compatible wrapper — prefer record_assurance_verdict in new code."""
    _assert_assurance_recording_allowed(assurance)
    if verdict is None:
        if passed is None:
            raise OrchestrationError("record_assurance_result requires verdict or passed")
        verdict = VERDICT_PASS if passed else VERDICT_FAIL
    from projectos.assurance_verdict import assurance_result_for_test

    result = assurance_result_for_test(
        verdict=verdict,
        assurance=assurance,
        summary=summary or f"legacy verdict {verdict}",
        findings=findings,
    )
    record_assurance_verdict(
        conn,
        assurance,
        result=result,
        evidence_ref=evidence_ref,
        create_defect_fn=create_defect_fn,
    )


def maybe_handoff_after_delivery(conn, job: OrchestrationJob) -> HandoffResult | None:
    if not is_valid_qa_candidate(job):
        return None
    existing = conn.execute(
        """
        SELECT COUNT(*) FROM qa_evidence WHERE delivery_job_id = ?
        """,
        (job.id,),
    ).fetchone()[0]
    if int(existing) > 0:
        return None
    return create_assurance_jobs_for_delivery(
        conn, job, candidate_git_sha=job.candidate_git_sha or ""
    )
