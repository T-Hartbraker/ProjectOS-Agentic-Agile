"""Authoritative operational failure evidence for recoverable control-plane errors."""

from __future__ import annotations

import re
from typing import Any

from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.execution_run import update_execution_run

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{10,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]+", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*\S+"),
)


def sanitize_operational_detail(detail: str, *, limit: int = 1500) -> str:
    text = str(detail or "").strip()
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text[:limit]


def record_operational_failure(
    conn,
    *,
    event_ctx: EventContext,
    component: str,
    operation: str,
    error_category: str,
    error_detail: str,
    recoverable: bool = True,
    phase: str = "intake",
    work_item_type: str | None = None,
    work_item_human_id: str | None = None,
    job_human_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist structured failure evidence and project it through the event/outbox path."""
    sanitized = sanitize_operational_detail(error_detail)
    evidence: dict[str, Any] = {
        "project_id": event_ctx.project_id,
        "run_id": event_ctx.run_id,
        "handoff_id": event_ctx.handoff_id,
        "phase": phase,
        "component": component,
        "operation": operation,
        "error_category": error_category,
        "error_detail": sanitized,
        "recoverable": recoverable,
    }
    if work_item_type:
        evidence["work_item_type"] = work_item_type
    if work_item_human_id:
        evidence["work_item_human_id"] = work_item_human_id
    if job_human_id:
        evidence["job_human_id"] = job_human_id
    if metadata:
        evidence.update(metadata)

    summary = (
        f"{component} failed during {operation}: {error_category}"
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="OPERATION_FAILED",
        summary=summary,
        actor_id=ACTOR_PM,
        phase=phase,
        detail=sanitized,
        detail_level="milestone",
        evidence=evidence,
        metadata=evidence,
        subscribers=("slack",),
    )
    if event_ctx.run_id:
        update_execution_run(
            conn,
            run_id=event_ctx.run_id,
            status="RUNNING",
            current_phase="execution_recovery" if recoverable else "terminal",
            current_agent="PM Agent",
            progress=40,
            result_summary=summary[:4000],
            evidence=evidence,
        )
    return evidence


def format_sponsor_failure_explanation(
    *,
    project_id: str,
    run_id: str,
    evidence: dict[str, Any],
    recovery_summary: str = "",
) -> str:
    category = str(evidence.get("error_category") or "operational_failure")
    operation = str(evidence.get("operation") or "execution")
    phase = str(evidence.get("phase") or "unknown")
    detail = str(evidence.get("error_detail") or "").strip()
    wi_type = str(evidence.get("work_item_type") or "").strip()
    wi_id = str(evidence.get("work_item_human_id") or "").strip()
    recoverable = evidence.get("recoverable")

    lines = [
        f"ProjectOS could not complete {operation} for `{project_id}` / `{run_id}`.",
        f"Phase: {phase}.",
        f"Failure category: {category}.",
    ]
    if wi_type and wi_id:
        lines.append(
            f"The PM execution plan referenced {wi_type} `{wi_id}` before authoritative "
            "project-control state was ready."
            if category == "missing_work_item_reference"
            else f"Affected work item: {wi_type} `{wi_id}`."
        )
    if detail:
        lines.append(f"Authoritative detail: {detail}")
    lines.append(
        "No downstream execution was allowed to proceed with invalid references."
        if category == "missing_work_item_reference"
        else "ProjectOS blocked progression until the failure is resolved."
    )
    if recoverable is True:
        lines.append(
            recovery_summary
            or "Recovery: PM scheduled a durable recovery action and the run remains active."
        )
    elif recoverable is False:
        lines.append("Recovery: this failure is not automatically recoverable.")
    return "\n".join(lines)
