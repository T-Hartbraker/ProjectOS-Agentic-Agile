"""Candidate identity for QA evaluation and remediation."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from projectos.execution_run import get_execution_run, update_execution_run

CANDIDATE_TYPE_GIT_SHA = "git_sha"


def get_run_candidate_state(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = get_execution_run(conn, run_id)
    if run is None or not run.evidence_json:
        return {}
    try:
        return json.loads(run.evidence_json)
    except json.JSONDecodeError:
        return {}


def set_run_active_candidate(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    candidate_id: str,
    candidate_type: str = CANDIDATE_TYPE_GIT_SHA,
    remediation_cycle: int = 0,
) -> None:
    state = get_run_candidate_state(conn, run_id)
    state.update(
        {
            "active_candidate_id": candidate_id,
            "active_candidate_type": candidate_type,
            "active_remediation_cycle": remediation_cycle,
        }
    )
    update_execution_run(conn, run_id=run_id, evidence=state)


def get_run_active_candidate(conn: sqlite3.Connection, run_id: str) -> str | None:
    return get_run_candidate_state(conn, run_id).get("active_candidate_id")


def latest_candidate_sha(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    run_id: str | None = None,
) -> str | None:
    if run_id:
        active = get_run_active_candidate(conn, run_id)
        if active:
            return active
    row = conn.execute(
        """
        SELECT candidate_git_sha FROM qa_evidence
        WHERE project_human_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    return str(row["candidate_git_sha"]) if row else None


def next_remediation_candidate_sha(source_candidate: str, remediation_cycle: int) -> str:
    return f"{source_candidate}-remediation-{remediation_cycle:03d}"
