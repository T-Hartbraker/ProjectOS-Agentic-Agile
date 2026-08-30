"""Recoverable RELEASE gate failures — return control to PM for corrective work."""

from __future__ import annotations

import sqlite3
from typing import Any

from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.release_readiness import ReleaseEvaluation
from projectos.run_next_actions import persist_run_next_action
from projectos.store import OrchestrationJob, create_job

_UNRECOVERABLE_MARKERS = (
    "repository identity mismatch",
    "cannot inspect evaluation workspace",
    "evaluation workspace is dirty",
    "projectctl context",
    "iteration status",
    "cannot advance iteration",
    "missing integrated candidate provenance",
    "policy",
    "authority",
    "remediation limit",
    "max remediation",
)


def is_correctable_release_block(reasons: list[str]) -> bool:
    if not reasons:
        return False
    joined = "; ".join(reasons).casefold()
    if any(marker in joined for marker in _UNRECOVERABLE_MARKERS):
        return False
    return True


def ensure_release_readiness_remediation(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    job: OrchestrationJob,
    release_eval: ReleaseEvaluation,
    repository_root: str,
) -> str:
    if not event_ctx.run_id:
        raise OrchestrationError("Release remediation requires run lineage")
    if not is_correctable_release_block(release_eval.reasons):
        raise OrchestrationError("Release block is not correctable by PM remediation")

    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="RELEASE_BLOCKED",
        summary="Release gate blocked; PM scheduled corrective work.",
        actor_id=ACTOR_PM,
        phase="release",
        detail_level="milestone",
        evidence={
            "reasons": release_eval.reasons,
            "readiness_report": str(release_eval.readiness_report_path),
            "correctable": True,
        },
    )

    retry_human_id = f"{job.human_id}__READINESS_RETRY"
    blocked_job = job

    retry = create_job(
        conn,
        human_id=retry_human_id,
        project_human_id=blocked_job.project_human_id,
        repository_root=repository_root,
        agent_role="PM",
        queue="PM",
        status="READY",
        iteration_human_id=blocked_job.iteration_human_id,
        run_id=event_ctx.run_id,
    )
    if blocked_job.source_candidate_sha:
        from projectos.store import set_job_source_provenance

        set_job_source_provenance(
            conn,
            retry.id,
            source_candidate_sha=blocked_job.source_candidate_sha,
        )
    conn.execute(
        """
        UPDATE orchestration_jobs
        SET status = 'READY', last_error = NULL, completed_at = NULL
        WHERE id = ? AND status = 'BLOCKED' AND queue = 'RELEASE'
        """,
        (blocked_job.id,),
    )
    action_id = persist_run_next_action(
        conn,
        run_id=event_ctx.run_id,
        project_id=blocked_job.project_human_id,
        action_type="PM_QUEUE",
        orchestration_job_id=retry.id,
        payload={
            "release_job_id": blocked_job.id,
            "release_job_human_id": blocked_job.human_id,
            "reasons": release_eval.reasons,
            "remediation_kind": "release_readiness",
        },
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="PM_REPLAN",
        summary="PM scheduled release readiness remediation.",
        actor_id=ACTOR_PM,
        phase="release",
        evidence={"next_action_id": action_id, "retry_job_id": retry.id},
    )
    return action_id


def handle_release_blocked_job(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    job: OrchestrationJob,
    release_eval: ReleaseEvaluation,
    repository_root: str,
) -> dict[str, Any]:
    if is_correctable_release_block(release_eval.reasons):
        action_id = ensure_release_readiness_remediation(
            conn,
            event_ctx=event_ctx,
            job=job,
            release_eval=release_eval,
            repository_root=repository_root,
        )
        return {"correctable": True, "next_action_id": action_id}
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="RELEASE_BLOCKED",
        summary="Release gate blocked (terminal).",
        actor_id=ACTOR_PM,
        phase="release",
        detail_level="milestone",
        evidence={
            "reasons": release_eval.reasons,
            "correctable": False,
        },
    )
    return {"correctable": False}
