"""Delivery → independent QA handoff and stale-evidence rejection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from projectos.constants import ASSURANCE_QUEUES, QUEUE_TO_ROLE
from projectos.db import connection
from projectos.delivery_evidence import is_valid_qa_candidate
from projectos.errors import OrchestrationError
from projectos.projectctl_bridge import create_defect
from projectos.store import (
    OrchestrationJob,
    add_job_dependency,
    append_run_event,
    create_job,
    get_job,
    set_job_source_provenance,
    utc_now_iso,
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
        )
        set_job_source_provenance(
            conn,
            job.id,
            source_delivery_job_id=delivery.id,
            source_candidate_sha=candidate_git_sha,
        )
        add_job_dependency(conn, job.id, delivery.id)
        conn.execute(
            """
            INSERT INTO qa_evidence (
                project_human_id, repository_root, delivery_job_id,
                assurance_job_id, candidate_git_sha, assurance_role, result
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                delivery.project_human_id,
                delivery.repository_root,
                delivery.id,
                job.id,
                candidate_git_sha,
                queue,
            ),
        )
        created.append(human_id)

    # QA Manager aggregation job waits on all assurance jobs.
    agg_id = f"{delivery.human_id}__QA_MANAGER"
    agg = create_job(
        conn,
        human_id=agg_id,
        project_human_id=delivery.project_human_id,
        repository_root=delivery.repository_root,
        agent_role="ASSURANCE_QUALITY",
        queue="ASSURANCE_QUALITY",
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
    for hid in created:
        # dependency by looking up ids
        pass
    for queue in REQUIRED_ASSURANCE:
        row = conn.execute(
            "SELECT id FROM orchestration_jobs WHERE human_id = ?",
            (f"{delivery.human_id}__{queue}",),
        ).fetchone()
        if row:
            add_job_dependency(conn, agg.id, int(row[0]))
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


def record_assurance_result(
    conn,
    assurance: OrchestrationJob,
    *,
    passed: bool,
    evidence_ref: str | None = None,
    create_defect_fn=None,
) -> None:
    """Record QA result; reject stale evidence against a newer candidate."""
    expected = assurance.source_candidate_sha
    if not expected:
        raise OrchestrationError("Assurance job missing source_candidate_sha")

    # Stale evidence: if delivery candidate moved forward, reject.
    if assurance.source_delivery_job_id is not None:
        delivery = get_job(conn, assurance.source_delivery_job_id)
        current = delivery.candidate_git_sha
        if current and current != expected:
            conn.execute(
                """
                UPDATE qa_evidence
                SET result = 'stale_rejected', evidence_ref = ?
                WHERE assurance_job_id = ? AND candidate_git_sha = ?
                """,
                (evidence_ref, assurance.id, expected),
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

    result = "pass" if passed else "fail"
    conn.execute(
        """
        UPDATE qa_evidence
        SET result = ?, evidence_ref = ?
        WHERE assurance_job_id = ? AND candidate_git_sha = ?
        """,
        (result, evidence_ref, assurance.id, expected),
    )

    if not passed:
        defect_id = None
        if create_defect_fn is not None:
            defect_out = create_defect_fn(
                Path(assurance.repository_root),
                title=f"QA failure on {assurance.queue} for {expected}",
                description=f"Assurance job {assurance.human_id} failed",
            )
            # parse Created BUG-…
            for line in (defect_out.stdout or "").splitlines():
                if "Created" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        defect_id = parts[1]
                        break
        rework_id = f"{assurance.human_id}__REWORK"
        rework = create_job(
            conn,
            human_id=rework_id,
            project_human_id=assurance.project_human_id,
            repository_root=assurance.repository_root,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            iteration_human_id=assurance.iteration_human_id,
            requires_worktree=True,
            identity_snapshot=json.loads(assurance.identity_snapshot_json)
            if assurance.identity_snapshot_json
            else None,
        )
        add_job_dependency(conn, rework.id, assurance.id)
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
            "qa.failed_rework",
            status="FAILED",
            message="Blocking QA failure; rework created",
            payload={"rework_job": rework_id, "defect": defect_id},
        )


def maybe_handoff_after_delivery(conn, job: OrchestrationJob) -> HandoffResult | None:
    if not is_valid_qa_candidate(job):
        return None
    # Avoid duplicate handoff
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
