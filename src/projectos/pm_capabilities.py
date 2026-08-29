"""PM Agent capability execution — single Sponsor mutation ingress."""

from __future__ import annotations

from typing import Any

from projectos.chatgpt_proposals import ProposalRecord, is_work_mutation
from projectos.domain_events import (
    ACTOR_PM,
    EventContext,
    emit_projectos_event,
    event_context_from_thread,
)
from projectos.errors import OrchestrationError
from projectos.execution_run import create_execution_run, get_execution_run, update_execution_run
from projectos.request_capability import classify_request
from projectos.services.context import ServiceContext
from projectos.slack_advisor_handoff import HandoffRequest
from projectos.slack_sponsor_format import SPONSOR_ACCEPTANCE, format_work_intake_execution, format_work_intake_preview
from projectos.sponsor_handoff import (
    create_sponsor_handoff,
    get_latest_thread_handoff,
    mark_handoff_accepted,
)


def _request_type_for_handoff(handoff: HandoffRequest) -> str:
    cap = classify_request(text=handoff.objective, fallback_objective=handoff.objective)
    if handoff.action_type in {"prepare_release", "package_release", "publish_release"}:
        return "RELEASE"
    return cap.request_type


def proposal_to_handoff(proposal: ProposalRecord) -> HandoffRequest:
    cap = classify_request(text=proposal.instruction, fallback_objective=proposal.instruction)
    return HandoffRequest(
        project_id=proposal.project_human_id,
        objective=proposal.instruction,
        action_type=proposal.action_type or cap.action_type,
        rationale="Converted from legacy approved proposal.",
        scope=proposal.scope or "",
        constraints="{}",
        acceptance_intent=SPONSOR_ACCEPTANCE,
        exclusions="",
        source_conversation_summary=f"Legacy proposal {proposal.proposal_id}",
    )


def ensure_pm_run_for_approved_proposal(
    ctx: ServiceContext,
    conn,
    *,
    proposal: ProposalRecord,
) -> tuple[str, EventContext]:
    """Convert legacy approved proposal into PM-owned handoff + ExecutionRun."""
    handoff = proposal_to_handoff(proposal)
    request_type = _request_type_for_handoff(handoff)
    thread_key = str(proposal.thread_ts or "")
    existing = get_latest_thread_handoff(
        conn,
        team_id=proposal.team_id,
        channel_id=proposal.channel_id,
        thread_ts=thread_key,
    )
    if existing and existing.run_id:
        run = get_execution_run(conn, existing.run_id)
        if run is not None:
            return run.run_id, event_context_from_thread(
                project_id=proposal.project_human_id,
                handoff_id=existing.handoff_id,
                run_id=run.run_id,
                team_id=proposal.team_id,
                channel_id=proposal.channel_id,
                thread_ts=thread_key,
            )

    cap = classify_request(text=handoff.objective, fallback_objective=handoff.objective)
    record = create_sponsor_handoff(
        conn,
        project_id=proposal.project_human_id,
        team_id=proposal.team_id,
        channel_id=proposal.channel_id,
        thread_ts=thread_key,
        sponsor_user_id=str(proposal.sponsor_user_id or ""),
        request_type=request_type,
        objective=handoff.objective,
        rationale=handoff.rationale,
        scope=handoff.scope,
        constraints_json=handoff.constraints if handoff.constraints.startswith("{") else "{}",
        acceptance_intent=handoff.acceptance_intent,
        exclusions=handoff.exclusions,
        desired_outputs_json=cap.desired_outputs_json(),
        conversation_summary=handoff.source_conversation_summary,
    )
    run = create_execution_run(
        conn,
        project_id=proposal.project_human_id,
        handoff_id=record.handoff_id,
        request_type=request_type,
        objective=handoff.objective,
    )
    mark_handoff_accepted(conn, handoff_id=record.handoff_id, run_id=run.run_id)
    event_ctx = event_context_from_thread(
        project_id=proposal.project_human_id,
        handoff_id=record.handoff_id,
        run_id=run.run_id,
        team_id=proposal.team_id,
        channel_id=proposal.channel_id,
        thread_ts=thread_key,
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="HANDOFF_ACCEPTED",
        summary=handoff.objective,
        actor_id=ACTOR_PM,
        detail=f"Legacy proposal {proposal.proposal_id} converted to PM run.",
        detail_level="milestone",
        metadata={"legacy_proposal_id": proposal.proposal_id, "request_type": request_type},
    )
    return run.run_id, event_ctx


def execute_pm_capability(
    ctx: ServiceContext,
    conn,
    *,
    handoff: HandoffRequest,
    run_id: str,
    project_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    proposal: ProposalRecord | None = None,
) -> str:
    """Execute a PM-selected capability for an existing ExecutionRun."""
    run = get_execution_run(conn, run_id)
    if run is None:
        raise OrchestrationError(f"Execution run {run_id!r} not found")

    event_ctx = event_context_from_thread(
        project_id=project_id,
        handoff_id=run.handoff_id,
        run_id=run_id,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
    )
    request_type = run.request_type
    cap = classify_request(text=handoff.objective, fallback_objective=handoff.objective)

    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="PLAN_STARTED",
        summary=f"PM executing {request_type} capability.",
        actor_id=ACTOR_PM,
        detail_level="milestone",
    )

    if request_type == "RELEASE" or cap.action_type in {
        "prepare_release",
        "package_release",
        "publish_release",
    }:
        from projectos.pm_agent import orchestrate_release_capability

        return orchestrate_release_capability(
            ctx,
            event_ctx=event_ctx,
            project_id=project_id,
            handoff=handoff,
        )

    if is_work_mutation(handoff.action_type) or request_type == "WORK":
        from projectos.slack_chatgpt import IntakeService

        kwargs = {
            "business_request": handoff.objective,
            "objective": handoff.objective,
            "acceptance": SPONSOR_ACCEPTANCE,
            "sponsor_authority": "approved",
        }
        if proposal is not None:
            result = IntakeService(ctx).submit(project_id, **kwargs)
            text = format_work_intake_execution(result, proposal)
        else:
            result = IntakeService(ctx).submit(project_id, **kwargs)
            text = f"Work intake submitted for {project_id}."
        from projectos.run_evidence import close_execution_run
        from projectos.run_outcomes import OUTCOME_SUCCESS

        close_execution_run(
            conn,
            event_ctx=event_ctx,
            outcome=OUTCOME_SUCCESS,
            summary="Work capability completed.",
            detail=text,
        )
        return text

    raise OrchestrationError(f"PM cannot execute unsupported capability: {request_type}")


def execute_approved_proposal_via_pm(
    ctx: ServiceContext,
    conn,
    *,
    proposal: ProposalRecord,
    run_id: str,
) -> str:
    handoff = proposal_to_handoff(proposal)
    return execute_pm_capability(
        ctx,
        conn,
        handoff=handoff,
        run_id=run_id,
        project_id=proposal.project_human_id,
        team_id=proposal.team_id,
        channel_id=proposal.channel_id,
        thread_ts=proposal.thread_ts,
        proposal=proposal,
    )
