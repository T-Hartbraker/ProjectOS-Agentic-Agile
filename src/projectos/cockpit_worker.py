"""Emit cockpit activity from orchestration workers into Sponsor runs."""

from __future__ import annotations

import sqlite3

from projectos.domain_events import (
    ACTOR_ARCHITECTURE,
    ACTOR_ASSURANCE,
    ACTOR_DEVELOPER,
    ACTOR_IMPROVEMENT,
    ACTOR_QA,
    ACTOR_RELEASE,
    ACTOR_SECURITY,
    EventContext,
    emit_projectos_event,
    lookup_event_context_for_job,
    lookup_event_context_for_project,
)
from projectos.store import OrchestrationJob

_ROLE_TO_ACTOR = {
    "ARCHITECTURE": ACTOR_ARCHITECTURE,
    "DELIVERY": ACTOR_DEVELOPER,
    "INTEGRATION": ACTOR_DEVELOPER,
    "ASSURANCE_FUNCTIONAL": ACTOR_QA,
    "ASSURANCE_INTEGRATION": ACTOR_QA,
    "ASSURANCE_SECURITY": ACTOR_SECURITY,
    "ASSURANCE_QUALITY": ACTOR_ASSURANCE,
    "RELEASE": ACTOR_RELEASE,
    "IMPROVEMENT": ACTOR_IMPROVEMENT,
}


def actor_id_for_job(job: OrchestrationJob) -> str:
    role = str(job.agent_role or job.queue or "").upper()
    if role in _ROLE_TO_ACTOR:
        return _ROLE_TO_ACTOR[role]
    if str(job.queue or "").startswith("ASSURANCE"):
        return ACTOR_QA
    return f"{role.lower()}-agent"


def emit_worker_cockpit_event(
    conn: sqlite3.Connection,
    job: OrchestrationJob,
    *,
    event_type: str,
    summary: str,
    detail: str = "",
    evidence: dict | None = None,
    detail_level: str = "normal",
    visibility: str = "SPONSOR",
    subscribers: tuple[str, ...] = ("slack",),
) -> None:
    base = lookup_event_context_for_job(conn, job.id)
    if base is None:
        base = lookup_event_context_for_project(conn, job.project_human_id)
    if base is None:
        return
    ctx = EventContext(
        project_id=base.project_id,
        handoff_id=base.handoff_id,
        run_id=base.run_id,
        job_id=job.human_id,
        work_item_id=job.work_item_human_id,
        correlation_id=base.run_id,
        slack_team_id=base.slack_team_id,
        slack_channel_id=base.slack_channel_id,
        slack_thread_ts=base.slack_thread_ts,
    )
    emit_projectos_event(
        conn,
        ctx=ctx,
        event_type=event_type,
        summary=summary,
        actor_id=actor_id_for_job(job),
        detail=detail,
        evidence=evidence,
        detail_level=detail_level,
        visibility=visibility,
        subscribers=subscribers,
        metadata={"queue": job.queue, "job_id": job.human_id},
    )


def emit_worker_terminal_event(
    conn: sqlite3.Connection,
    job: OrchestrationJob,
    *,
    status: str,
    error: str = "",
    outcome: str | None = None,
) -> None:
    if status == "SUCCEEDED":
        event_type = "WORK_COMPLETED"
        summary = f"{job.human_id} completed successfully."
        detail_level = "milestone"
        evidence = {"outcome": outcome} if outcome else None
    elif status == "BLOCKED":
        event_type = "WORK_BLOCKED"
        summary = f"{job.human_id} blocked."
        detail_level = "milestone"
        evidence = {"error_category": "blocked", "retryable": True, "sponsor_impact": error[:200]}
    elif status == "CANCELLED":
        event_type = "WORK_FAILED"
        summary = f"{job.human_id} cancelled."
        detail_level = "milestone"
        evidence = {"error_category": "cancelled", "retryable": False, "sponsor_impact": error[:200]}
    else:
        event_type = "WORK_FAILED"
        summary = f"{job.human_id} failed."
        detail_level = "milestone"
        evidence = {"error_category": "failed", "retryable": True, "sponsor_impact": error[:200]}
    emit_worker_cockpit_event(
        conn,
        job,
        event_type=event_type,
        summary=summary,
        detail=error[:500] if error else "",
        evidence=evidence,
        detail_level=detail_level,
    )


def record_worker_failure(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    error: str,
    output_ref: str | None = None,
    blocked: bool = False,
) -> OrchestrationJob:
    from projectos.store import mark_failure

    final = mark_failure(
        conn,
        job_id,
        error=error,
        output_ref=output_ref,
        blocked=blocked,
    )
    if final.status in {"FAILED", "BLOCKED"}:
        emit_worker_terminal_event(conn, final, status=final.status, error=error)
    return final
