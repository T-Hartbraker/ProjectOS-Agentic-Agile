"""Event truthfulness guards — domain events require real persisted evidence."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from projectos.errors import OrchestrationError


def require_persisted_work(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    orchestration_job_id: int | None,
) -> None:
    row = conn.execute(
        "SELECT 1 FROM remediation_work WHERE work_item_id = ?", (work_item_id,)
    ).fetchone()
    if row is None:
        raise OrchestrationError(
            f"AGENT_ASSIGNED requires persisted remediation work {work_item_id!r}"
        )
    if orchestration_job_id is not None:
        job = conn.execute(
            "SELECT 1 FROM orchestration_jobs WHERE id = ?", (orchestration_job_id,)
        ).fetchone()
        if job is None:
            raise OrchestrationError(
                f"AGENT_ASSIGNED references missing orchestration job {orchestration_job_id}"
            )


def require_work_completion_evidence(evidence: dict[str, Any] | None) -> None:
    if not evidence or not evidence.get("work_item_id"):
        raise OrchestrationError("WORK_COMPLETED requires work_item_id evidence")
    if not (evidence.get("target_candidate_id") or evidence.get("candidate_git_sha")):
        raise OrchestrationError("WORK_COMPLETED requires candidate evidence")


def require_installer_artifact(path: str | Path) -> None:
    p = Path(path)
    if not p.is_file():
        raise OrchestrationError("INSTALLER_BUILT requires an existing installer artifact")
    if p.suffix.lower() == ".json" and "placeholder" in p.name.lower():
        raise OrchestrationError("INSTALLER_BUILT cannot reference placeholder artifact")


def require_publication_record(evidence: dict[str, Any] | None) -> None:
    if not evidence or not (evidence.get("url") or evidence.get("release_record_id")):
        raise OrchestrationError("RELEASE_PUBLISHED requires publication record evidence")
