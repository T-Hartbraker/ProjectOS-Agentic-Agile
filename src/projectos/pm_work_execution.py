"""PM-authorized work execution without redundant Sponsor approval."""

from __future__ import annotations

from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.execution_run import update_execution_run
from projectos.run_evidence import pause_run_for_sponsor_decision
from projectos.run_next_actions import persist_run_next_action
from projectos.services.context import ServiceContext
from projectos.slack_advisor_handoff import HandoffRequest
from projectos.slack_sponsor_format import SPONSOR_ACCEPTANCE
from projectos.sponsor_execution_authority import SponsorExecutionAuthority
from projectos.store import get_job_by_human_id, list_eligible_ready_jobs


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
    from projectos.intake import IntakeService

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
        detail_level="milestone",
        metadata={
            "authority_source": authority.authority_source,
            "scheduled_job_ids": scheduled,
        },
    )
    return (
        f"PM accepted Sponsor-authorized work for `{project_id}` and scheduled "
        f"{len(scheduled)} executable next action(s)."
    )
