"""PM Agent ingress — orchestration authority for Sponsor handoffs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from projectos.domain_events import (
    ACTOR_DEVELOPER,
    ACTOR_PM,
    EventContext,
    emit_projectos_event,
    event_context_from_thread,
)
from projectos.chatgpt_proposals import create_proposal, is_work_mutation
from projectos.delivery.contract import (
    delivery_contract_missing_evidence,
    load_delivery_contract,
    orchestration_blocker_from_message,
)
from projectos.delivery.store import list_delivery_artifacts
from projectos.db import connection
from projectos.errors import OrchestrationError
from projectos.execution_run import create_execution_run, get_execution_run, update_execution_run
from projectos.packaging.registry import detect_packaging_adapter
from projectos.registry import load_registry
from projectos.request_capability import CapabilityContract, classify_request
from projectos.services.context import ServiceContext
from projectos.slack_activity_blocks import handoff_accepted_blocks
from projectos.slack_advisor_handoff import HandoffRequest
from projectos.sponsor_directive import classify_sponsor_ingress, directive_requires_replan
from projectos.sponsor_handoff import (
    create_sponsor_handoff,
    get_latest_thread_handoff,
    mark_handoff_accepted,
    mark_handoff_rejected,
)

RELEASE_PHASES = [
    "RELEASE READINESS",
    "SOURCE_GATE",
    "QA_GATE",
    "BUILD_GATE",
    "PACKAGE_GATE",
    "CHECKSUM_GATE",
    "SBOM_GATE",
    "SIGNATURE_GATE",
    "PUBLICATION_GATE",
    "DELIVERY_GATE",
]


@dataclass(frozen=True)
class PmHandoffResult:
    handoff_id: str
    run_id: str
    request_type: str
    advisor_note: str
    projectos_text: str
    projectos_blocks: list[dict[str, Any]]
    execution_evidence: str | None = None


def compose_server_handoff(
    *,
    project_id: str,
    sponsor_message: str,
    thread_messages: list[str] | None = None,
    advisor_summary: str = "",
) -> HandoffRequest:
    combined = "\n".join(
        [msg for msg in (thread_messages or []) + [sponsor_message, advisor_summary] if msg.strip()]
    )
    cap = classify_request(text=combined, fallback_objective=sponsor_message)
    return HandoffRequest(
        project_id=project_id,
        objective=cap.objective[:2000],
        action_type=cap.action_type,
        rationale=advisor_summary[:500] if advisor_summary else "",
        scope="",
        constraints=json.dumps(cap.constraints, sort_keys=True),
        acceptance_intent=_acceptance_from_outputs(cap),
        exclusions="",
        source_conversation_summary=combined[:1500],
    )


def _acceptance_from_outputs(cap: CapabilityContract) -> str:
    if not cap.desired_outputs:
        return ""
    parts = [f"{key}={value}" for key, value in cap.desired_outputs.items()]
    return "Desired outputs: " + ", ".join(parts)


def _request_type_for_handoff(handoff: HandoffRequest) -> str:
    cap = classify_request(text=handoff.objective, fallback_objective=handoff.objective)
    if handoff.action_type in {"prepare_release", "package_release", "publish_release"}:
        return "RELEASE"
    return cap.request_type


def _apply_active_run_directive(
    ctx: ServiceContext,
    conn,
    *,
    handoff: HandoffRequest,
    project_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    sponsor_user_id: str,
    advisor_text: str,
    existing_handoff,
    active_run,
    request_type: str,
) -> PmHandoffResult:
    """Route a Sponsor follow-up into the existing active run."""
    from projectos.run_outcomes import STATUS_WAITING_FOR_SPONSOR, normalize_waiting_status

    run_id = active_run.run_id
    handoff_id = existing_handoff.handoff_id
    thread = event_context_from_thread(
        project_id=project_id,
        handoff_id=handoff_id,
        run_id=run_id,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
    )
    if normalize_waiting_status(active_run.status) == STATUS_WAITING_FOR_SPONSOR:
        update_execution_run(conn, run_id=run_id, status="RUNNING")

    directive_evidence = {
        "directive_kind": "ACTIVE_RUN_DIRECTIVE",
        "objective": handoff.objective,
        "request_type": request_type,
        "sponsor_user_id": sponsor_user_id,
    }
    emit_projectos_event(
        conn,
        ctx=thread,
        event_type="SPONSOR_DIRECTIVE_RECEIVED",
        summary=handoff.objective[:500],
        actor_id=ACTOR_PM,
        detail_level="milestone",
        evidence=directive_evidence,
    )
    emit_projectos_event(
        conn,
        ctx=thread,
        event_type="PLAN_UPDATED",
        summary="PM updated the active run plan from Sponsor directive.",
        actor_id=ACTOR_PM,
        detail_level="normal",
        evidence=directive_evidence,
    )
    if directive_requires_replan(handoff, request_type=request_type):
        emit_projectos_event(
            conn,
            ctx=thread,
            event_type="PM_REPLAN",
            summary="PM replanned work in response to Sponsor directive.",
            actor_id=ACTOR_PM,
            phase="PLANNING",
            detail_level="normal",
            evidence=directive_evidence,
        )
        agent_id = ACTOR_DEVELOPER if request_type in {"DEFECT", "WORK"} else ACTOR_PM
        emit_projectos_event(
            conn,
            ctx=thread,
            event_type="AGENT_ASSIGNED",
            summary=f"Assigned: {agent_id} for Sponsor directive.",
            actor_id=ACTOR_PM,
            phase="PLANNING",
            metadata={"agent_id": agent_id},
            evidence=directive_evidence,
        )

    projectos_text = (
        f"*ProjectOS PM — SPONSOR DIRECTIVE RECEIVED*\n"
        f"Run: `{run_id}`\n"
        f"Project: `{project_id}`\n"
        f"Directive: {handoff.objective[:200]}"
    )
    advisor_note = (
        f"{advisor_text.strip()}\n\n"
        "Your follow-up was routed into the active run. "
        "The PM Agent will adjust the plan and continue execution."
    ).strip()
    blocks = handoff_accepted_blocks(
        handoff_id=handoff_id,
        project_id=project_id,
        request_type=request_type,
        run_id=run_id,
        objective=handoff.objective,
    )

    if request_type == "RELEASE":
        conn.commit()
        try:
            evidence = orchestrate_release_capability(
                ctx,
                event_ctx=thread,
                project_id=project_id,
                handoff=handoff,
            )
        except OrchestrationError as exc:
            _ensure_release_run_closed(
                ctx,
                event_ctx=thread,
                project_id=project_id,
                error=exc,
            )
            raise
        return PmHandoffResult(
            handoff_id=handoff_id,
            run_id=run_id,
            request_type=request_type,
            advisor_note=advisor_note,
            projectos_text=projectos_text,
            projectos_blocks=blocks,
            execution_evidence=evidence,
        )

    return PmHandoffResult(
        handoff_id=handoff_id,
        run_id=run_id,
        request_type=request_type,
        advisor_note=advisor_note,
        projectos_text=projectos_text,
        projectos_blocks=blocks,
    )


def accept_sponsor_handoff(
    ctx: ServiceContext,
    conn,
    *,
    handoff: HandoffRequest,
    project_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    sponsor_user_id: str,
    advisor_text: str = "",
) -> PmHandoffResult:
    existing = get_latest_thread_handoff(
        conn, team_id=team_id, channel_id=channel_id, thread_ts=thread_ts
    )
    request_type = _request_type_for_handoff(handoff)
    active_run = None
    if existing and existing.status == "ACCEPTED_BY_PM" and existing.run_id:
        active_run = get_execution_run(conn, existing.run_id)

    if (
        active_run
        and active_run.status not in {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED", "ESCALATED"}
        and classify_sponsor_ingress(
            handoff=handoff,
            existing_handoff=existing,
            active_run=active_run,
            request_type=request_type,
        )
        == "ACTIVE_RUN_DIRECTIVE"
    ):
        return _apply_active_run_directive(
            ctx,
            conn,
            handoff=handoff,
            project_id=project_id,
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            sponsor_user_id=sponsor_user_id,
            advisor_text=advisor_text,
            existing_handoff=existing,
            active_run=active_run,
            request_type=request_type,
        )

    record = create_sponsor_handoff(
        conn,
        project_id=project_id,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        sponsor_user_id=sponsor_user_id,
        request_type=request_type,
        objective=handoff.objective,
        rationale=handoff.rationale,
        scope=handoff.scope,
        constraints_json=handoff.constraints if handoff.constraints.startswith("{") else "{}",
        acceptance_intent=handoff.acceptance_intent,
        exclusions=handoff.exclusions,
        desired_outputs_json=classify_request(
            text=handoff.objective, fallback_objective=handoff.objective
        ).desired_outputs_json(),
        conversation_summary=handoff.source_conversation_summary,
    )
    run = create_execution_run(
        conn,
        project_id=project_id,
        handoff_id=record.handoff_id,
        request_type=request_type,
        objective=handoff.objective,
    )
    accepted = mark_handoff_accepted(conn, handoff_id=record.handoff_id, run_id=run.run_id)
    if accepted is None:
        mark_handoff_rejected(conn, handoff_id=record.handoff_id, reason="PM acceptance failed")
        raise OrchestrationError("PM Agent could not accept handoff")

    thread = event_context_from_thread(
        project_id=project_id,
        handoff_id=record.handoff_id,
        run_id=run.run_id,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
    )
    emit_projectos_event(
        conn,
        ctx=thread,
        event_type="HANDOFF_ACCEPTED",
        summary=handoff.objective,
        actor_id=ACTOR_PM,
        detail=f"Handoff {record.handoff_id} accepted",
        detail_level="milestone",
        metadata={"request_type": request_type},
    )

    blocks = handoff_accepted_blocks(
        handoff_id=record.handoff_id,
        project_id=project_id,
        request_type=request_type,
        run_id=run.run_id,
        objective=handoff.objective,
    )
    projectos_text = (
        f"*ProjectOS PM — HANDOFF ACCEPTED*\n"
        f"Handoff: `{record.handoff_id}`\n"
        f"Project: `{project_id}`\n"
        f"Request: `{request_type}`\n"
        f"Run: `{run.run_id}`\n"
        f"State: `PLANNING`"
    )
    advisor_note = (
        f"{advisor_text.strip()}\n\n"
        "I've sent a governed handoff to the ProjectOS PM Agent. "
        "Operational updates will appear below as execution proceeds."
    ).strip()

    if request_type == "RELEASE":
        conn.commit()
        try:
            evidence = orchestrate_release_capability(
                ctx,
                event_ctx=thread,
                project_id=project_id,
                handoff=handoff,
            )
        except OrchestrationError as exc:
            _ensure_release_run_closed(
                ctx,
                event_ctx=thread,
                project_id=project_id,
                error=exc,
            )
            raise
        return PmHandoffResult(
            handoff_id=record.handoff_id,
            run_id=run.run_id,
            request_type=request_type,
            advisor_note=advisor_note,
            projectos_text=projectos_text,
            projectos_blocks=blocks,
            execution_evidence=evidence,
        )

    return _orchestrate_work_handoff(
        ctx,
        conn,
        handoff=handoff,
        record_handoff_id=record.handoff_id,
        run=run,
        thread=thread,
        sponsor_user_id=sponsor_user_id,
        advisor_note=advisor_note,
        projectos_text=projectos_text,
        projectos_blocks=blocks,
        request_type=request_type,
    )


def _emit_plan(
    conn,
    *,
    thread: EventContext,
    objective: str,
    phases: list[tuple[str, str]],
    current: str,
) -> None:
    emit_projectos_event(
        conn,
        ctx=thread,
        event_type="PM_PLAN_CREATED",
        summary=objective,
        actor_id=ACTOR_PM,
        detail=current,
        detail_level="milestone",
        metadata={"phases": phases},
    )


def _resolve_repo_root(ctx: ServiceContext, project_id: str) -> Path | None:
    try:
        registry = load_registry(ctx.registry_path)
        entry = registry.get(project_id)
        if entry and entry.repository_root:
            return Path(entry.repository_root).resolve()
    except Exception:
        return None
    return None


def _format_terminal_run_evidence(evidence: dict[str, Any]) -> str:
    run_id = evidence.get("run_id") or ""
    status = evidence.get("terminal_status") or "BLOCKED"
    lines = [
        f"*ProjectOS PM — RUN {status}*",
        f"Run: `{run_id}`",
    ]
    failure = evidence.get("failure") or {}
    if failure.get("blocker_type"):
        lines.append(f"Blocker: `{failure['blocker_type']}`")
    if failure.get("path"):
        lines.append(f"Path: `{failure['path']}`")
    if failure.get("required_action"):
        lines.append(f"Required action: {failure['required_action']}")
    qa = evidence.get("qa") or failure.get("qa") or {}
    if qa.get("reviews_total") is not None:
        lines.append(
            f"QA: {qa.get('reviews_completed', 0)} completed, "
            f"{qa.get('reviews_need_attention', 0)} need attention "
            f"of {qa['reviews_total']} reviews"
        )
    if failure.get("auto_remediation"):
        rem = failure["auto_remediation"]
        if rem.get("available"):
            lines.append(
                "Auto-remediation: ProjectOS may generate a governed delivery contract draft "
                "after Sponsor confirms repository and signing policy."
            )
    return "\n".join(lines)


def _ensure_release_run_closed(
    ctx: ServiceContext,
    *,
    event_ctx: EventContext,
    project_id: str,
    error: OrchestrationError,
) -> None:
    from projectos.run_outcomes import OUTCOME_UNRECOVERABLE_TECHNICAL
    from projectos.run_evidence import close_execution_run, build_terminal_evidence

    if not event_ctx.run_id:
        return
    with connection(ctx.db_path) as conn:
        existing = build_terminal_evidence(conn, run_id=event_ctx.run_id)
        if existing.get("terminal_status") in {"BLOCKED", "FAILED", "COMPLETED", "CANCELLED"}:
            return
        repo_root = _resolve_repo_root(ctx, project_id)
        failure = orchestration_blocker_from_message(str(error), repo_root=repo_root)
        close_execution_run(
            conn,
            event_ctx=event_ctx,
            outcome=OUTCOME_UNRECOVERABLE_TECHNICAL,
            summary="Release orchestration failed.",
            detail=str(error)[:2000],
            failure=failure,
        )


def orchestrate_release_capability(
    ctx: ServiceContext,
    *,
    event_ctx: EventContext,
    project_id: str,
    handoff: HandoffRequest,
) -> str:
    from projectos.delivery.service import DeliveryService

    phases = [(name, "pending") for name in RELEASE_PHASES]
    phases[0] = (RELEASE_PHASES[0], "active")
    with connection(ctx.db_path) as plan_conn:
        _emit_plan(
            plan_conn,
            thread=event_ctx,
            objective=handoff.objective,
            phases=phases,
            current="PM Agent routing to universal delivery pipeline.",
        )
        emit_projectos_event(
            plan_conn,
            ctx=event_ctx,
            event_type="PLAN_STARTED",
            summary="Release workflow started.",
            actor_id=ACTOR_PM,
            detail_level="milestone",
        )

    svc = DeliveryService(ctx)
    repo_root = _resolve_repo_root(ctx, project_id)
    with connection(ctx.db_path) as lookup_conn:
        release_human_id = _next_release_human_id(lookup_conn, project_id)
        from projectos.delivery.release_version import resolve_release_version

        version = resolve_release_version(
            lookup_conn, project_id=project_id, repo_root=repo_root
        )
    adapter_id = "unknown"
    if repo_root:
        try:
            adapter_id = detect_packaging_adapter(repo_root, load_delivery_contract(repo_root))
        except OrchestrationError:
            adapter_id = "unknown"
        except Exception:
            adapter_id = "unknown"

    with connection(ctx.db_path) as qa_conn:
        from projectos.pm_remediation import run_qa_with_remediation

        qa_result = run_qa_with_remediation(
            qa_conn, event_ctx=event_ctx, project_id=project_id
        )

    if qa_result.escalated:
        from projectos.run_evidence import build_terminal_evidence

        with connection(ctx.db_path) as conn:
            evidence = build_terminal_evidence(conn, run_id=event_ctx.run_id or "")
        return _format_terminal_run_evidence(evidence)

    gate = qa_result.gate
    if gate != "PASSED":
        return (
            f"*ProjectOS PM — QA gate `{gate}`*\n"
            f"Run: `{event_ctx.run_id}`\n"
            "PM is managing remediation; run remains active."
        )

    with connection(ctx.db_path) as prep_conn:
        emit_projectos_event(
            prep_conn,
            ctx=event_ctx,
            event_type="PHASE_CHANGED",
            summary="Current phase: RELEASE_PREPARATION",
            actor_id=ACTOR_PM,
            phase="RELEASE_PREPARATION",
            detail_level="normal",
            metadata={"assigned_agent": "delivery-agent"},
        )
        emit_projectos_event(
            prep_conn,
            ctx=event_ctx,
            event_type="AGENT_ASSIGNED",
            summary="Assigned: delivery-agent",
            actor_id=ACTOR_PM,
            phase="RELEASE_PREPARATION",
            detail_level="normal",
            metadata={"agent_id": "delivery-agent"},
        )

    try:
        prepared = svc.prepare_release(
            project_id,
            release_human_id=release_human_id,
            version=version,
            sponsor_user_id=None,
            event_context=event_ctx,
        )
    except OrchestrationError as exc:
        failure = orchestration_blocker_from_message(str(exc), repo_root=repo_root)
        with connection(ctx.db_path) as block_conn:
            if failure.get("blocker_type") == "DELIVERY_CONTRACT_MISSING" and repo_root:
                from projectos.pm_delivery_remediation import attempt_delivery_contract_remediation

                remediation = attempt_delivery_contract_remediation(
                    block_conn,
                    event_ctx=event_ctx,
                    repo_root=repo_root,
                    failure=failure,
                )
                block_conn.commit()
                if remediation.sponsor_pause:
                    return (
                        f"*ProjectOS PM — WAITING FOR SPONSOR*\n"
                        f"Run: `{event_ctx.run_id}`\n"
                        f"{remediation.message}"
                    )
                if remediation.recovered:
                    try:
                        prepared = svc.prepare_release(
                            project_id,
                            release_human_id=release_human_id,
                            version=version,
                            sponsor_user_id=None,
                            event_context=event_ctx,
                        )
                    except OrchestrationError as retry_exc:
                        retry_failure = orchestration_blocker_from_message(
                            str(retry_exc), repo_root=repo_root
                        )
                        with connection(ctx.db_path) as retry_conn:
                            emit_projectos_event(
                                retry_conn,
                                ctx=event_ctx,
                                event_type="RELEASE_PREPARATION_BLOCKED",
                                summary=str(retry_exc)[:500],
                                actor_id=ACTOR_PM,
                                phase="RELEASE_PREPARATION",
                                status="BLOCKED",
                                detail_level="milestone",
                                evidence=retry_failure,
                            )
                        return (
                            f"*ProjectOS PM — RELEASE PREPARATION BLOCKED*\n"
                            f"Run: `{event_ctx.run_id}`\n"
                            "PM is managing remediation; run remains active."
                        )
                else:
                    emit_projectos_event(
                        block_conn,
                        ctx=event_ctx,
                        event_type="RELEASE_PREPARATION_BLOCKED",
                        summary=str(exc)[:500],
                        actor_id=ACTOR_PM,
                        phase="RELEASE_PREPARATION",
                        status="BLOCKED",
                        detail_level="milestone",
                        evidence=failure,
                    )
                    return (
                        f"*ProjectOS PM — RELEASE PREPARATION BLOCKED*\n"
                        f"Run: `{event_ctx.run_id}`\n"
                        "PM is managing remediation; run remains active."
                    )
            else:
                emit_projectos_event(
                    block_conn,
                    ctx=event_ctx,
                    event_type="RELEASE_PREPARATION_BLOCKED",
                    summary=str(exc)[:500],
                    actor_id=ACTOR_PM,
                    phase="RELEASE_PREPARATION",
                    status="BLOCKED",
                    detail_level="milestone",
                    evidence=failure,
                )
        return (
            f"*ProjectOS PM — RELEASE PREPARATION BLOCKED*\n"
            f"Run: `{event_ctx.run_id}`\n"
            "PM is managing remediation; run remains active."
        )

    release_record_id = str(prepared["release_record_id"])
    try:
        svc.package_release(release_record_id, event_context=event_ctx)
    except OrchestrationError as exc:
        failure = orchestration_blocker_from_message(str(exc), repo_root=repo_root)
        with connection(ctx.db_path) as block_conn:
            emit_projectos_event(
                block_conn,
                ctx=event_ctx,
                event_type="PACKAGE_FAILED",
                summary=str(exc)[:500],
                actor_id=ACTOR_PM,
                phase="PACKAGE_GATE",
                status="FAILED",
                detail_level="milestone",
                evidence=failure,
            )
        return (
            f"*ProjectOS PM — PACKAGE FAILED*\n"
            f"Run: `{event_ctx.run_id}`\n"
            "PM is managing remediation; run remains active."
        )

    stub_installer = adapter_id == "python_desktop"
    with connection(ctx.db_path) as lookup_conn:
        artifacts = list_delivery_artifacts(lookup_conn, release_record_id)
    installer = next((a for a in artifacts if a["artifact_type"] == "installer"), None)
    evidence_text = _format_release_evidence(
        run_id=event_ctx.run_id or "",
        release_human_id=release_human_id,
        release_record_id=release_record_id,
        installer=installer,
        download_url=None,
        stub_installer=stub_installer,
        adapter_id=adapter_id,
    )
    if stub_installer:
        from projectos.pm_delivery_remediation import handle_capability_gap

        with connection(ctx.db_path) as gap_conn:
            handle_capability_gap(
                gap_conn,
                event_ctx=event_ctx,
                gap={
                    "blocker_type": "INSTALLER_BACKEND_MISSING",
                    "reason": "python_desktop adapter produces placeholder installer only",
                    "retryable": True,
                    "phase": "INSTALLER",
                },
            )
        return (
            f"{evidence_text}\n\n"
            "PM is managing installer capability remediation; run remains active."
        )

    published_url = None
    try:
        published = svc.publish_release(release_record_id, event_context=event_ctx)
        published_url = published.get("github_release_url") or published.get("download_url")
    except OrchestrationError as pub_exc:
        with connection(ctx.db_path) as pub_conn:
            emit_projectos_event(
                pub_conn,
                ctx=event_ctx,
                event_type="PUBLICATION_FAILED",
                summary=str(pub_exc)[:500],
                actor_id=ACTOR_PM,
                phase="PUBLICATION_GATE",
                status="FAILED",
                detail_level="milestone",
                evidence={"release_record_id": release_record_id, "reason": str(pub_exc)[:500]},
            )
        return (
            f"*ProjectOS PM — PUBLICATION FAILED*\n"
            f"Run: `{event_ctx.run_id}`\n"
            f"Release: `{release_human_id}`\n"
            "PM is managing remediation; run remains active."
        )

    return _format_release_evidence(
        run_id=event_ctx.run_id or "",
        release_human_id=release_human_id,
        release_record_id=release_record_id,
        installer=installer,
        download_url=published_url,
        stub_installer=False,
        adapter_id=adapter_id,
    )


def _orchestrate_release_run(
    ctx: ServiceContext,
    *,
    thread: EventContext,
    project_id: str,
    handoff: HandoffRequest,
) -> str:
    return orchestrate_release_capability(
        ctx, event_ctx=thread, project_id=project_id, handoff=handoff
    )


def _orchestrate_work_handoff(
    ctx,
    conn,
    *,
    handoff: HandoffRequest,
    record_handoff_id: str,
    run,
    thread: EventContext,
    sponsor_user_id: str,
    advisor_note: str,
    projectos_text: str,
    projectos_blocks: list,
    request_type: str,
) -> PmHandoffResult:
    phases = [("Intake validation", "active"), ("Governed preview", "pending"), ("Execution", "pending")]
    _emit_plan(
        conn,
        thread=thread,
        objective=handoff.objective,
        phases=phases,
        current="PM Agent creating governed preview.",
    )
    from projectos.run_outcomes import OUTCOME_SPONSOR_DECISION_REQUIRED, run_status_for_outcome
    from projectos.run_evidence import pause_run_for_sponsor_decision

    proposal = create_proposal(
        conn,
        team_id=thread.slack_team_id,
        channel_id=thread.slack_channel_id,
        thread_ts=thread.slack_thread_ts,
        sponsor_user_id=sponsor_user_id,
        project_human_id=thread.project_id,
        intent=handoff.action_type,
        instruction=handoff.to_instruction(),
    )
    update_execution_run(
        conn,
        run_id=run.run_id,
        status=run_status_for_outcome(OUTCOME_SPONSOR_DECISION_REQUIRED)
        if is_work_mutation(proposal.action_type)
        else "RUNNING",
        current_phase="preview",
        current_agent="PM Agent",
        progress=25,
    )
    emit_projectos_event(
        conn,
        ctx=thread,
        event_type="AGENT_ASSIGNED",
        summary=f"Proposal `{proposal.proposal_id}` created for Sponsor approval.",
        actor_id=ACTOR_PM,
        detail_level="normal",
        metadata={"proposal_id": proposal.proposal_id},
        subscribers=(),
    )
    preview_text = None
    if is_work_mutation(proposal.action_type):
        from projectos.slack_chatgpt import _generate_and_persist_preview

        preview_text = _generate_and_persist_preview(ctx, conn, proposal)
        pause_run_for_sponsor_decision(
            conn,
            event_ctx=thread,
            summary="Sponsor approval required before execution.",
            detail=f"Proposal `{proposal.proposal_id}` awaiting Sponsor decision.",
            evidence={"proposal_id": proposal.proposal_id},
        )
        emit_projectos_event(
            conn,
            ctx=thread,
            event_type="SPONSOR_DECISION_REQUIRED",
            summary="Sponsor approval required before execution.",
            actor_id=ACTOR_PM,
            detail_level="milestone",
            metadata={"proposal_id": proposal.proposal_id},
        )
    return PmHandoffResult(
        handoff_id=record_handoff_id,
        run_id=run.run_id,
        request_type=request_type,
        advisor_note=advisor_note,
        projectos_text=projectos_text,
        projectos_blocks=projectos_blocks,
        execution_evidence=preview_text,
    )


def _next_release_human_id(conn, project_id: str) -> str:
    row = conn.execute(
        """
        SELECT release_human_id FROM delivery_releases
        WHERE project_human_id = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if not row:
        return "REL-001"
    match = re.match(r"REL-(\d+)", str(row["release_human_id"] or ""))
    if not match:
        return "REL-001"
    return f"REL-{int(match.group(1)) + 1:03d}"


def _format_release_evidence(
    *,
    run_id: str,
    release_human_id: str,
    release_record_id: str,
    installer: dict | None,
    download_url: str | None,
    stub_installer: bool,
    adapter_id: str,
) -> str:
    lines = [
        f"*Release evidence — `{run_id}`*",
        f"Release ID: `{release_human_id}`",
        f"Record: `{release_record_id}`",
        f"Adapter: `{adapter_id}`",
    ]
    if installer:
        lines.extend(
            [
                f"Artifact: `{installer.get('artifact_name')}`",
                f"SHA-256: `{installer.get('sha256')}`",
                f"Size: {installer.get('size_bytes')} bytes",
                f"Signature: {installer.get('signature_status')}",
            ]
        )
    if stub_installer:
        lines.append("Status: PACKAGE PIPELINE COMPLETE — PRODUCTION INSTALLER NOT AVAILABLE")
    elif download_url:
        lines.append(f"Download: {download_url}")
    else:
        lines.append("Status: Packaged — publication pending or blocked")
    return "\n".join(lines)
