"""Derive ExecutionRun state from authoritative ProjectOS domain events."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from projectos.execution_run import get_execution_run, update_execution_run
from projectos.run_outcomes import (
    EVENT_WAITING_FOR_SPONSOR,
    STATUS_WAITING_FOR_SPONSOR,
    TERMINAL_RUN_EVENTS,
    is_pm_terminal_run_event,
    is_terminal_run_status,
)

# PM-authoritative terminal transitions only.
_PM_TERMINAL_PROGRESSION = {
    "RUN_COMPLETED": ("COMPLETED", "pm-agent", 100),
    "RUN_BLOCKED": ("BLOCKED", None, 85),
    "RUN_ESCALATED": ("ESCALATED", "pm-agent", 88),
    "RUN_CANCELLED": ("CANCELLED", "pm-agent", 0),
    "RUN_FAILED": ("FAILED", None, 90),  # legacy alias
}

# Nonterminal progression — phase/agent/progress only; status unchanged unless paused.
_OPERATIONAL_PROGRESSION = {
    "HANDOFF_ACCEPTED": ("PLANNING", "pm-agent", 5),
    "PM_PLAN_CREATED": ("PLANNING", "pm-agent", 15),
    "PM_REPLAN": ("PLANNING", "pm-agent", 20),
    "AGENT_ASSIGNED": ("RUNNING", None, 25),
    "WORK_STARTED": ("RUNNING", None, 35),
    "WORK_PROGRESS": ("RUNNING", None, 50),
    "WORK_COMPLETED": ("RUNNING", None, 55),
    "PACKAGE_STARTED": ("RUNNING", "delivery-agent", 55),
    "PACKAGE_COMPLETED": ("RUNNING", "delivery-agent", 75),
    "INSTALLER_BUILT": ("RUNNING", "delivery-agent", 80),
    "INSTALLER_SIGNED": ("RUNNING", "delivery-agent", 85),
    "QA_STARTED": ("RUNNING", "qa-agent", 45),
    "QA_GATE_PASSED": ("RUNNING", "qa-agent", 50),
    "QA_RETEST_STARTED": ("RUNNING", "qa-agent", 48),
    "QA_GATE_FAILED": ("QA_GATE", "qa-agent", 40),
    "REMEDIATION_REQUIRED": ("REMEDIATION", "pm-agent", 42),
    "REMEDIATION_STARTED": ("REMEDIATION", None, 45),
    "PACKAGE_FAILED": ("PACKAGE_GATE", "delivery-agent", 65),
    "PUBLICATION_FAILED": ("PUBLICATION_GATE", "release-agent", 88),
    "WORK_FAILED": ("WORK", None, 45),
    "WORK_BLOCKED": ("WORK", None, 45),
    "RELEASE_PREPARATION_BLOCKED": ("RELEASE_PREPARATION", "delivery-agent", 60),
    "CAPABILITY_GAP_DETECTED": ("CAPABILITY", "pm-agent", 55),
    "RELEASE_PUBLISHED": ("RUNNING", "release-agent", 90),
    "SPONSOR_DIRECTIVE_RECEIVED": ("RUNNING", "pm-agent", 30),
    "PLAN_UPDATED": ("RUNNING", "pm-agent", 32),
    "WAITING_FOR_SPONSOR": (STATUS_WAITING_FOR_SPONSOR, "pm-agent", 40),
    "SPONSOR_DECISION_REQUIRED": (STATUS_WAITING_FOR_SPONSOR, "pm-agent", 40),
}


def apply_event_to_run(conn: sqlite3.Connection, *, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
    if not run_id:
        return
    run = get_execution_run(conn, run_id)
    if run is None:
        return
    if is_terminal_run_status(run.status) and event_type not in TERMINAL_RUN_EVENTS:
        return

    phase = payload.get("phase") or payload.get("event_type", "").lower()
    agent = payload.get("actor_id")
    progress = payload.get("progress")
    status: str | None = None

    if is_pm_terminal_run_event(event_type):
        mapping = _PM_TERMINAL_PROGRESSION.get(event_type)
        if mapping:
            mapped_status, mapped_agent, mapped_progress = mapping
            status = mapped_status
            agent = agent or mapped_agent
            progress = progress if progress is not None else mapped_progress
    else:
        mapping = _OPERATIONAL_PROGRESSION.get(event_type)
        if mapping:
            mapped_phase, mapped_agent, mapped_progress = mapping
            phase = mapped_phase
            agent = agent or mapped_agent
            progress = progress if progress is not None else mapped_progress
            if event_type in {EVENT_WAITING_FOR_SPONSOR, "SPONSOR_DECISION_REQUIRED"}:
                status = STATUS_WAITING_FOR_SPONSOR

    evidence = payload.get("evidence")
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError:
            evidence = {"detail": evidence}
    update_execution_run(
        conn,
        run_id=run_id,
        status=str(status) if status else None,
        current_phase=str(phase)[:120] if phase else None,
        current_agent=str(agent)[:80] if agent else None,
        progress=int(progress) if progress is not None else None,
        result_summary=str(payload.get("summary") or "")[:4000] or None,
        evidence=evidence if isinstance(evidence, dict) else None,
    )


def run_status_summary(conn: sqlite3.Connection, *, run_id: str) -> dict[str, Any]:
    run = get_execution_run(conn, run_id)
    if run is None:
        return {}
    events = conn.execute(
        """
        SELECT event_type, summary, actor_role, occurred_at, evidence_json
        FROM projectos_events
        WHERE run_id = ? AND visibility = 'SPONSOR'
        ORDER BY occurred_at DESC
        LIMIT 12
        """,
        (run_id,),
    ).fetchall()
    return {
        "run_id": run.run_id,
        "project_id": run.project_id,
        "objective": run.objective,
        "status": run.status,
        "current_phase": run.current_phase,
        "current_agent": run.current_agent,
        "progress": run.progress,
        "result_summary": run.result_summary,
        "recent_events": [
            {
                "event_type": row["event_type"],
                "summary": row["summary"],
                "actor_role": row["actor_role"],
                "occurred_at": row["occurred_at"],
            }
            for row in events
        ],
    }
