"""No-inert-run diagnostic — every active run must have a durable next action."""

from __future__ import annotations

import sqlite3
from typing import Any

from projectos.execution_run import get_execution_run
from projectos.run_next_actions import has_durable_next_action, list_active_next_actions
from projectos.run_outcomes import is_terminal_run_status


def classify_run_next_action(conn: sqlite3.Connection, *, run_id: str, project_id: str) -> dict[str, Any]:
    run = get_execution_run(conn, run_id)
    if run is None or is_terminal_run_status(run.status):
        return {"state": "terminal", "run_id": run_id}

    if run.status == "WAITING_FOR_SPONSOR":
        return {"state": "SPONSOR_WAIT", "run_id": run_id}

    actions = list_active_next_actions(conn, run_id=run_id)
    if actions:
        return {
            "state": actions[0]["action_type"],
            "run_id": run_id,
            "actions": [a["action_id"] for a in actions],
        }

    if has_durable_next_action(conn, run_id=run_id, project_id=project_id):
        return {"state": "EXECUTABLE", "run_id": run_id}

    return {"state": "RUN_INERT", "run_id": run_id}


def assert_run_has_next_action(conn: sqlite3.Connection, *, run_id: str, project_id: str) -> None:
    action = classify_run_next_action(conn, run_id=run_id, project_id=project_id)
    if action["state"] == "RUN_INERT":
        raise AssertionError(f"Run {run_id} has no durable next action")


def assert_nonterminal_run_has_durable_next_action(
    conn: sqlite3.Connection, *, run_id: str, project_id: str
) -> dict[str, Any]:
    """Reusable assertion for integration tests — events alone do not count."""
    run = get_execution_run(conn, run_id)
    if run is None or is_terminal_run_status(run.status):
        return {"state": "terminal", "run_id": run_id}
    assert_run_has_next_action(conn, run_id=run_id, project_id=project_id)
    return classify_run_next_action(conn, run_id=run_id, project_id=project_id)
