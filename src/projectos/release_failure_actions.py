"""Durable next actions for recoverable release failures."""

from __future__ import annotations

import sqlite3
from typing import Any

from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.pm_delivery_remediation import handle_capability_gap
from projectos.remediation_store import create_remediation_work
from projectos.run_next_actions import persist_run_next_action


def ensure_package_failure_next_action(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    project_id: str,
    repository_root: str,
    failure: dict[str, Any],
    release_record_id: str | None = None,
    service_ctx=None,
) -> str:
    blocker = str(failure.get("blocker_type") or "").upper()
    if blocker in {"CAPABILITY_GAP", "INSTALLER_BACKEND_MISSING"}:
        handle_capability_gap(
            conn,
            event_ctx=event_ctx,
            gap=failure,
            project_id=project_id,
            repository_root=repository_root,
            service_ctx=service_ctx,
        )
        from projectos.execution_run import get_execution_run
        from projectos.remediation_recovery import list_outstanding_remediation_work
        from projectos.store import create_job

        run_id = event_ctx.run_id or project_id
        outstanding = list_outstanding_remediation_work(conn, run_id=run_id)
        if outstanding:
            work = outstanding[0]
            return persist_run_next_action(
                conn,
                run_id=run_id,
                project_id=project_id,
                action_type="REMEDIATION_WORK",
                remediation_work_id=work.work_item_id,
                orchestration_job_id=work.orchestration_job_id,
                payload={"failure": failure, "release_record_id": release_record_id},
            )
        run = get_execution_run(conn, run_id)
        if run is not None and str(run.status) == "WAITING_FOR_SPONSOR":
            sponsor_job = create_job(
                conn,
                human_id=f"{run_id}__SPONSOR_DECISION",
                project_human_id=project_id,
                repository_root=repository_root,
                agent_role="PM",
                queue="PM",
                status="READY",
                run_id=run_id,
            )
            return persist_run_next_action(
                conn,
                run_id=run_id,
                project_id=project_id,
                action_type="PM_QUEUE",
                orchestration_job_id=sponsor_job.id,
                payload={"failure": failure, "release_record_id": release_record_id, "sponsor_wait": True},
            )
        raise OrchestrationError(
            "Package capability gap remediation did not produce executable next action"
        )
    work = create_remediation_work(
        conn,
        run_id=event_ctx.run_id or project_id,
        project_id=project_id,
        remediation_cycle=1,
        finding_ids=[blocker or "PACKAGE_FAILED"],
        assigned_agent="delivery-agent",
        objective=f"Remediate package failure: {failure.get('reason') or blocker}",
        acceptance_criteria="Package gate passes with verified artifacts.",
        source_candidate_id=None,
        repository_root=repository_root,
        assignment_reason="Package failure remediation",
        findings=[failure],
        execution_queue="DELIVERY",
    )
    action_id = persist_run_next_action(
        conn,
        run_id=event_ctx.run_id or project_id,
        project_id=project_id,
        action_type="REMEDIATION_WORK",
        remediation_work_id=work.work_item_id,
        orchestration_job_id=work.orchestration_job_id,
        payload={"failure": failure, "release_record_id": release_record_id},
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="PM_REPLAN",
        summary="PM scheduled package failure remediation.",
        actor_id=ACTOR_PM,
        phase="PACKAGE_GATE",
        evidence={"work_item_id": work.work_item_id, "next_action_id": action_id},
    )
    return action_id


def ensure_publication_failure_next_action(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    project_id: str,
    repository_root: str,
    failure: dict[str, Any],
    release_record_id: str | None = None,
) -> str:
    from projectos.store import create_job

    retry_job = create_job(
        conn,
        human_id=f"{event_ctx.run_id}__PUBLICATION_RETRY__{release_record_id or 'unknown'}",
        project_human_id=project_id,
        repository_root=repository_root,
        agent_role="DELIVERY",
        queue="DELIVERY",
        status="RETRY_WAIT",
        base_git_sha="",
        run_id=event_ctx.run_id,
    )
    action_id = persist_run_next_action(
        conn,
        run_id=event_ctx.run_id or project_id,
        project_id=project_id,
        action_type="SCHEDULED_RETRY",
        orchestration_job_id=retry_job.id,
        payload={"failure": failure, "release_record_id": release_record_id},
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="PM_REPLAN",
        summary="PM scheduled publication retry.",
        actor_id=ACTOR_PM,
        phase="PUBLICATION_GATE",
        evidence={"next_action_id": action_id, "retry_job_id": retry_job.id},
    )
    return action_id
