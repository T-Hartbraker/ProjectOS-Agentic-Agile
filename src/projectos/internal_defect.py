"""Governed internal ProjectOS defect routing."""

from __future__ import annotations

import sqlite3
import traceback
from typing import Any

from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.run_evidence import pause_run_for_sponsor_decision


def handle_internal_defect(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    error: Exception,
    component: str,
    operation: str,
    project_id: str,
    in_project_scope: bool = False,
    service_ctx=None,
    worker=None,
    repository_root: str | None = None,
) -> dict[str, Any]:
    """Route internal software defects to structured PM work or authority escalation."""
    evidence: dict[str, Any] = {
        "error_type": type(error).__name__,
        "message": str(error)[:1000],
        "component": component,
        "triggering_operation": operation,
        "run_id": event_ctx.run_id,
        "project_id": project_id,
        "retryable": True,
        "trace": traceback.format_exc()[-2000:],
    }
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="INTERNAL_DEFECT_DETECTED",
        summary=f"Internal defect in {component}: {type(error).__name__}",
        actor_id=ACTOR_PM,
        phase="REMEDIATION",
        detail_level="milestone",
        evidence=evidence,
    )
    if in_project_scope:
        from projectos.remediation_executor import execute_remediation_work
        from projectos.remediation_store import count_remediation_cycles, create_remediation_work
        from projectos.registry import load_registry
        from projectos.paths import DEFAULT_REGISTRY_PATH

        registry = load_registry(DEFAULT_REGISTRY_PATH)
        entry = registry.get(project_id)
        repo_root = repository_root or (str(entry.repository_root) if entry else "")
        cycle = count_remediation_cycles(conn, run_id=event_ctx.run_id or project_id) + 1
        work = create_remediation_work(
            conn,
            run_id=event_ctx.run_id or project_id,
            project_id=project_id,
            remediation_cycle=cycle,
            finding_ids=["INTERNAL-DEFECT"],
            assigned_agent="developer-agent",
            objective=f"Fix internal defect: {evidence['message'][:200]}",
            acceptance_criteria="Defect no longer reproduces; QA validates fix.",
            source_candidate_id=None,
            repository_root=repo_root or "/",
            assignment_reason="Internal defect within project repository scope",
            findings=[evidence],
        )
        evidence["remediation_work_item_id"] = work.work_item_id
        outcome = execute_remediation_work(
            conn,
            work=work,
            event_ctx=event_ctx,
            project_id=project_id,
            repository_root=repo_root or "/",
            service_ctx=service_ctx,
            worker=worker,
        )
        if outcome.status == "COMPLETED" and outcome.target_candidate_id:
            from projectos.qa_retest import execute_qa_retest

            execute_qa_retest(
                conn,
                event_ctx=event_ctx,
                project_id=project_id,
                repository_root=repo_root or "/",
                candidate_id=outcome.target_candidate_id,
                run_id=event_ctx.run_id,
                remediation_cycle=cycle,
                source_remediation_job_id=work.orchestration_job_id,
                service_ctx=service_ctx,
            )
        return evidence

    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="CROSS_PROJECT_REMEDIATION_REQUIRED",
        summary="Defect is outside current project authority; Sponsor decision required.",
        actor_id=ACTOR_PM,
        phase="sponsor_decision",
        evidence=evidence,
    )
    pause_run_for_sponsor_decision(
        conn,
        event_ctx=event_ctx,
        summary="Cross-project remediation required for internal ProjectOS defect.",
        evidence=evidence,
    )
    return evidence
