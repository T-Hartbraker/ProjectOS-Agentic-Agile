"""PM-authorized work execution without redundant Sponsor approval."""

from __future__ import annotations

from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.execution_run import update_execution_run
from projectos.intake import IntakeService
from projectos.registry import load_registry
from projectos.run_evidence import pause_run_for_sponsor_decision
from projectos.run_next_actions import persist_run_next_action
from projectos.services.context import ServiceContext
from projectos.slack_advisor_handoff import HandoffRequest
from projectos.slack_sponsor_format import SPONSOR_ACCEPTANCE
from projectos.sponsor_execution_authority import SponsorExecutionAuthority
from projectos.store import create_job, get_job_by_human_id, list_eligible_ready_jobs


def _schedule_work_execution_recovery(
    conn,
    *,
    ctx: ServiceContext,
    thread: EventContext,
    run_id: str,
    project_id: str,
    authority: SponsorExecutionAuthority,
    reason: str,
) -> str:
    registry = load_registry(ctx.registry_path)
    entry = registry.get(project_id)
    if entry is None or not entry.repository_root:
        raise OrchestrationError(
            f"Cannot schedule work execution recovery for unknown project {project_id!r}"
        )
    repository_root = str(entry.repository_root)
    retry_job = create_job(
        conn,
        human_id=f"{run_id}__WORK_EXEC_SCHEDULE",
        project_human_id=project_id,
        repository_root=repository_root,
        agent_role="PM",
        queue="PM",
        status="READY",
        run_id=run_id,
    )
    action_id = persist_run_next_action(
        conn,
        run_id=run_id,
        project_id=project_id,
        action_type="EXECUTABLE_JOB",
        orchestration_job_id=retry_job.id,
        payload={
            "reason": reason,
            "authority_source": authority.authority_source,
        },
    )
    update_execution_run(
        conn,
        run_id=run_id,
        status="RUNNING",
        current_phase="execution_recovery",
        current_agent="PM Agent",
        progress=45,
        result_summary="PM scheduled recovery after authorized intake produced no executable jobs.",
    )
    emit_projectos_event(
        conn,
        ctx=thread,
        event_type="PM_REPLAN",
        summary="PM scheduled work execution recovery.",
        actor_id=ACTOR_PM,
        phase="execution_recovery",
        detail_level="milestone",
        metadata={
            "reason": reason,
            "next_action_id": action_id,
            "retry_job_id": retry_job.id,
        },
    )
    update_execution_run(
        conn,
        run_id=run_id,
        status="RUNNING",
        current_phase="execution_recovery",
        current_agent="PM Agent",
        progress=45,
        result_summary="PM scheduled recovery after authorized intake produced no executable jobs.",
    )
    return (
        f"PM scheduled work execution recovery for `{project_id}` "
        f"after authorized intake produced no executable jobs."
    )


def begin_authorized_work_execution(
    ctx: ServiceContext,
    conn,
    *,
    handoff: HandoffRequest,
    run_id: str,
    project_id: str,
    thread: EventContext,
    authority: SponsorExecutionAuthority,
) -> str:
    """Submit governed work intake and schedule durable executable next actions."""

    explicit_new_project = authority.authority_source == "explicit_new_project"
    kwargs = {
        "business_request": handoff.objective,
        "objective": handoff.objective,
        "acceptance": handoff.acceptance_intent or SPONSOR_ACCEPTANCE,
        "sponsor_authority": authority.sponsor_authority or "approved",
        "explicit_new_project": explicit_new_project,
    }

    conn.commit()
    result = IntakeService(ctx).submit(project_id, **kwargs)

    if result.status == "needs_sponsor_decision":
        pause_run_for_sponsor_decision(
            conn,
            event_ctx=thread,
            summary="Sponsor decision required before execution.",
            detail="; ".join(
                item.get("question", item.get("code", "decision"))
                for item in (result.decision_requests or [])
            )
            or "Sponsor-reserved decision required.",
            evidence={
                "authority_source": authority.authority_source,
                "decision_requests": result.decision_requests,
            },
        )
        emit_projectos_event(
            conn,
            ctx=thread,
            event_type="SPONSOR_DECISION_REQUIRED",
            summary="Sponsor decision required before execution.",
            actor_id=ACTOR_PM,
            detail_level="milestone",
            metadata={"decision_requests": result.decision_requests},
        )
        return "Sponsor decision required before execution."

    if result.status not in {"submitted"}:
        raise OrchestrationError(
            f"Authorized work execution failed during intake submit: {result.error or result.status}"
        )

    created_ids: list[int] = []
    for human_id in result.jobs_created:
        job = get_job_by_human_id(conn, human_id)
        if job is None:
            continue
        conn.execute(
            "UPDATE orchestration_jobs SET run_id = ? WHERE id = ?",
            (run_id, job.id),
        )
        created_ids.append(job.id)

    scheduled: list[int] = []
    for job in list_eligible_ready_jobs(conn, project_human_id=project_id):
        if job.human_id not in result.jobs_created and job.id not in created_ids:
            continue
        if not job.run_id:
            conn.execute(
                "UPDATE orchestration_jobs SET run_id = ? WHERE id = ?",
                (run_id, job.id),
            )
        persist_run_next_action(
            conn,
            run_id=run_id,
            project_id=project_id,
            action_type="EXECUTABLE_JOB",
            orchestration_job_id=job.id,
            payload={
                "authority_source": authority.authority_source,
                "authorization_scope": authority.authorization_scope,
                "job_human_id": job.human_id,
            },
        )
        scheduled.append(job.id)

    if not scheduled:
        return _schedule_work_execution_recovery(
            conn,
            ctx=ctx,
            thread=thread,
            run_id=run_id,
            project_id=project_id,
            authority=authority,
            reason="no_executable_jobs_after_authorized_intake",
        )

    update_execution_run(
        conn,
        run_id=run_id,
        status="RUNNING",
        current_phase="execution",
        current_agent="PM Agent",
        progress=50,
        result_summary="Sponsor-authorized work execution started.",
    )
    emit_projectos_event(
        conn,
        ctx=thread,
        event_type="PLAN_STARTED",
        summary="PM accepted Sponsor-authorized work and scheduled execution.",
        actor_id=ACTOR_PM,
        detail_level="milestone",
        metadata={
            "authority_source": authority.authority_source,
            "authorization_scope": authority.authorization_scope,
            "jobs_created": result.jobs_created,
            "scheduled_job_ids": scheduled,
        },
    )
    emit_projectos_event(
        conn,
        ctx=thread,
        event_type="WORK_EXECUTION_AUTHORIZED",
        summary="Execution authorized by authenticated Sponsor action.",
        actor_id=ACTOR_PM,
        phase="execution",
        detail_level="milestone",
        metadata={
            "authority_source": authority.authority_source,
            "scheduled_job_ids": scheduled,
        },
    )
    update_execution_run(
        conn,
        run_id=run_id,
        status="RUNNING",
        current_phase="execution",
        current_agent="PM Agent",
        progress=50,
        result_summary="Sponsor-authorized work execution started.",
    )
    return (
        f"PM accepted Sponsor-authorized work for `{project_id}` and scheduled "
        f"{len(scheduled)} executable next action(s)."
    )
