"""No-inert-run diagnostic — every active run must have a next action."""

from __future__ import annotations

import sqlite3
from typing import Any

from projectos.execution_run import get_execution_run
from projectos.remediation_recovery import list_outstanding_remediation_work
from projectos.run_outcomes import is_terminal_run_status
from projectos.store import list_jobs_for_project

_ACTIVE_JOB_STATUSES = frozenset({"READY", "LEASED", "RUNNING", "RETRY_WAIT", "QUEUED"})
_EXECUTABLE_WORK = frozenset({"ASSIGNED", "RUNNING"})


def classify_run_next_action(conn: sqlite3.Connection, *, run_id: str, project_id: str) -> dict[str, Any]:
    run = get_execution_run(conn, run_id)
    if run is None or is_terminal_run_status(run.status):
        return {"state": "terminal", "run_id": run_id}

    jobs = list_jobs_for_project(conn, project_id, statuses=_ACTIVE_JOB_STATUSES)
    if jobs:
        return {
            "state": "EXECUTABLE",
            "run_id": run_id,
            "jobs": [j.human_id for j in jobs[:5]],
        }

    outstanding = list_outstanding_remediation_work(conn, run_id=run_id)
    active_work = [w for w in outstanding if w.status in _EXECUTABLE_WORK]
    if active_work:
        return {
            "state": "EXECUTABLE",
            "run_id": run_id,
            "work_items": [w.work_item_id for w in active_work],
        }

    if run.status == "WAITING_FOR_SPONSOR":
        return {"state": "SPONSOR_WAIT", "run_id": run_id}

    retry_jobs = list_jobs_for_project(conn, project_id, statuses={"RETRY_WAIT"})
    if retry_jobs:
        return {"state": "RETRY_WAIT", "run_id": run_id, "jobs": [j.human_id for j in retry_jobs]}

    pm_action = conn.execute(
        """
        SELECT event_type FROM projectos_events
        WHERE run_id = ? AND event_type IN (
            'PM_REPLAN', 'REMEDIATION_REQUIRED', 'QA_INCONCLUSIVE',
            'ASSURANCE_RETRY_SCHEDULED', 'INTERNAL_DEFECT_DETECTED'
        )
        ORDER BY occurred_at DESC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if pm_action:
        return {"state": "PM_ACTION", "run_id": run_id, "last_event": pm_action["event_type"]}

    return {"state": "RUN_INERT", "run_id": run_id}


def assert_run_has_next_action(conn: sqlite3.Connection, *, run_id: str, project_id: str) -> None:
    action = classify_run_next_action(conn, run_id=run_id, project_id=project_id)
    if action["state"] == "RUN_INERT":
        raise AssertionError(f"Run {run_id} has no durable next action")
