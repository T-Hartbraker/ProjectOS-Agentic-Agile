"""Verify authoritative QA evidence before release preparation."""

from __future__ import annotations

import sqlite3
from typing import Any

from projectos.errors import OrchestrationError
from projectos.qa_gate import collect_qa_gate_facts


def require_qa_gate_passed(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    candidate_git_sha: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    facts = collect_qa_gate_facts(
        conn,
        project_id=project_id,
        candidate_git_sha=candidate_git_sha,
        run_id=run_id,
    )
    gate = str(facts.get("gate") or "PENDING")
    if gate != "PASSED":
        raise OrchestrationError(
            f"QA gate {gate} for candidate {candidate_git_sha}; release preparation refused"
        )
    return facts
