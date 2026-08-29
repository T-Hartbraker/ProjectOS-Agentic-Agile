"""Candidate identity for QA evaluation and remediation."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from projectos.errors import OrchestrationError
from projectos.execution_run import get_execution_run, update_execution_run

CANDIDATE_TYPE_GIT_SHA = "git_sha"
CANDIDATE_TYPE_WORK_PRODUCT = "work_product"


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


def get_run_active_candidate_type(conn: sqlite3.Connection, run_id: str) -> str:
    return str(
        get_run_candidate_state(conn, run_id).get("active_candidate_type")
        or CANDIDATE_TYPE_GIT_SHA
    )


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


def git_object_exists(repository_root: str, candidate_id: str) -> bool:
    repo = Path(repository_root)
    if not (repo / ".git").exists():
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{candidate_id}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def validate_candidate_identity(
    candidate_id: str,
    *,
    candidate_type: str,
    repository_root: str,
) -> None:
    if not candidate_id:
        raise OrchestrationError("Remediation candidate identity is required")
    if candidate_type == CANDIDATE_TYPE_GIT_SHA:
        if "-remediation-" in candidate_id:
            raise OrchestrationError(
                f"Candidate {candidate_id!r} is not a valid git SHA; synthetic IDs are prohibited"
            )
        if not git_object_exists(repository_root, candidate_id):
            raise OrchestrationError(
                f"Candidate git SHA {candidate_id!r} does not exist in {repository_root}"
            )
        return
    if candidate_type == CANDIDATE_TYPE_WORK_PRODUCT:
        if not candidate_id.startswith("WP-"):
            raise OrchestrationError(
                f"Work product candidate {candidate_id!r} must use WP- prefix"
            )
        return
    raise OrchestrationError(f"Unsupported candidate_type {candidate_type!r}")
