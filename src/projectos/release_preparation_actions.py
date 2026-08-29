"""Durable next actions for release preparation failures."""

from __future__ import annotations

import sqlite3
from typing import Any

from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.pm_delivery_remediation import attempt_delivery_contract_remediation, handle_capability_gap
from projectos.remediation_store import create_remediation_work
from projectos.run_next_actions import persist_run_next_action


def ensure_release_preparation_next_action(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    project_id: str,
    repository_root: str,
    failure: dict[str, Any],
    service_ctx=None,
) -> str:
    blocker = str(failure.get("blocker_type") or "").upper()
    run_id = event_ctx.run_id or project_id

    if blocker == "DELIVERY_CONTRACT_MISSING":
        remediation = attempt_delivery_contract_remediation(
            conn,
            event_ctx=event_ctx,
            repo_root=repository_root,
            failure=failure,
        )
        if remediation.sponsor_pause:
            from projectos.execution_run import update_execution_run

            update_execution_run(conn, run_id=run_id, status="WAITING_FOR_SPONSOR")
            return persist_run_next_action(
                conn,
                run_id=run_id,
                project_id=project_id,
                action_type="SPONSOR_WAIT",
                payload={"failure": failure, "reason": remediation.message},
            )
        if remediation.recovered:
            return persist_run_next_action(
                conn,
                run_id=run_id,
                project_id=project_id,
                action_type="REMEDIATION_WORK",
                payload={"failure": failure, "recovered": True},
            )

    if blocker in {"CAPABILITY_GAP", "INSTALLER_BACKEND_MISSING"}:
        handle_capability_gap(
            conn,
            event_ctx=event_ctx,
            gap=failure,
            project_id=project_id,
            repository_root=repository_root,
            service_ctx=service_ctx,
        )
        return persist_run_next_action(
            conn,
            run_id=run_id,
            project_id=project_id,
            action_type="REMEDIATION_WORK",
            payload={"failure": failure},
        )

    work = create_remediation_work(
        conn,
        run_id=run_id,
        project_id=project_id,
        remediation_cycle=1,
        finding_ids=[blocker or "RELEASE_PREPARATION_BLOCKED"],
        assigned_agent="delivery-agent",
        objective=f"Remediate release preparation failure: {failure.get('reason') or blocker}",
        acceptance_criteria="Release preparation succeeds with valid candidate and contract.",
        source_candidate_id=None,
        repository_root=repository_root,
        assignment_reason="Release preparation blocked",
        findings=[failure],
        execution_queue="DELIVERY",
    )
    action_id = persist_run_next_action(
        conn,
        run_id=run_id,
        project_id=project_id,
        action_type="REMEDIATION_WORK",
        remediation_work_id=work.work_item_id,
        orchestration_job_id=work.orchestration_job_id,
        payload={"failure": failure},
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="PM_REPLAN",
        summary="PM scheduled release preparation remediation.",
        actor_id=ACTOR_PM,
        phase="RELEASE_PREPARATION",
        evidence={"work_item_id": work.work_item_id, "next_action_id": action_id},
    )
    return action_id
