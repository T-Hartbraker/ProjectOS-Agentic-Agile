"""Event truthfulness guards — domain events require real persisted evidence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from projectos.delivery.gates import GATE_STATUS_PASSED
from projectos.delivery.store import get_delivery_release, list_gate_statuses
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


def require_publication_record(evidence: dict[str, Any] | None, *, ctx=None) -> None:
    release_record_id = (evidence or {}).get("release_record_id") or (
        getattr(ctx, "release_record_id", None) if ctx else None
    )
    if not release_record_id:
        raise OrchestrationError("RELEASE_PUBLISHED requires release_record_id evidence")


def require_qa_gate_evidence(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    candidate_git_sha: str | None,
    run_id: str | None,
    evidence: dict[str, Any] | None = None,
) -> None:
    if evidence and str(evidence.get("gate") or "") == "PASSED":
        return
    from projectos.qa_gate import collect_qa_gate_facts

    facts = collect_qa_gate_facts(
        conn,
        project_id=project_id,
        candidate_git_sha=candidate_git_sha or (evidence or {}).get("candidate_git_sha"),
        run_id=run_id or (evidence or {}).get("run_id"),
    )
    if str(facts.get("gate") or "") != "PASSED":
        raise OrchestrationError("QA_GATE_PASSED requires authoritative QA gate evidence")


def require_verify_gate_passed(conn: sqlite3.Connection, *, release_record_id: str) -> None:
    gates = list_gate_statuses(conn, release_record_id)
    if gates.get("VERIFY_GATE") != GATE_STATUS_PASSED:
        raise OrchestrationError("RELEASE_VERIFIED requires VERIFY_GATE PASSED evidence")


def require_sponsor_outcome_satisfied(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    handoff_id: str | None,
    objective: str,
    request_type: str | None,
    release_record_id: str | None,
    candidate_git_sha: str | None,
) -> None:
    from projectos.sponsor_outcome import evaluate_sponsor_outcome

    evaluation = evaluate_sponsor_outcome(
        conn,
        run_id=run_id,
        handoff_id=handoff_id,
        objective=objective,
        request_type=request_type,
        release_record_id=release_record_id,
        candidate_git_sha=candidate_git_sha,
    )
    if not evaluation.satisfied:
        raise OrchestrationError(
            f"RUN_COMPLETED blocked: missing outcomes {evaluation.missing_outputs}"
        )


def validate_event_truthfulness(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    ctx,
    evidence: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> None:
    """Validate sensitive events at canonical emission boundary."""
    if event_type == "AGENT_ASSIGNED":
        work_item_id = (evidence or {}).get("work_item_id") or (metadata or {}).get("work_item_id")
        job_id = (evidence or {}).get("orchestration_job_id") or (metadata or {}).get("orchestration_job_id")
        if work_item_id:
            require_persisted_work(
                conn,
                work_item_id=str(work_item_id),
                orchestration_job_id=int(job_id) if job_id else None,
            )
        elif (metadata or {}).get("capability") or (metadata or {}).get("agent_id") or (metadata or {}).get("assigned_agent") or (metadata or {}).get("proposal_id"):
            return
        elif (evidence or {}).get("agent_id") or (evidence or {}).get("assigned_agent"):
            return
        else:
            raise OrchestrationError("AGENT_ASSIGNED requires work or explicit assignment metadata")
    elif event_type == "WORK_COMPLETED":
        require_work_completion_evidence(evidence)
    elif event_type == "QA_GATE_PASSED":
        require_qa_gate_evidence(
            conn,
            project_id=ctx.project_id,
            candidate_git_sha=(evidence or {}).get("candidate_git_sha"),
            run_id=ctx.run_id,
            evidence=evidence,
        )
    elif event_type == "RELEASE_PUBLISHED":
        require_publication_record(evidence, ctx=ctx)
        release_record_id = (evidence or {}).get("release_record_id") or ctx.release_record_id
        if release_record_id:
            record = get_delivery_release(conn, release_record_id=str(release_record_id))
            if record is None:
                raise OrchestrationError("RELEASE_PUBLISHED requires persisted release record")
    elif event_type == "RELEASE_VERIFIED":
        release_record_id = (evidence or {}).get("release_record_id") or ctx.release_record_id
        if not release_record_id:
            raise OrchestrationError("RELEASE_VERIFIED requires release_record_id")
        require_verify_gate_passed(conn, release_record_id=str(release_record_id))
    elif event_type == "RUN_COMPLETED":
        run = conn.execute(
            "SELECT handoff_id, objective, request_type FROM execution_runs WHERE run_id = ?",
            (ctx.run_id,),
        ).fetchone()
        if run is None:
            raise OrchestrationError("RUN_COMPLETED requires execution run")
        if str(run["request_type"] or "").upper() != "RELEASE":
            return
        require_sponsor_outcome_satisfied(
            conn,
            run_id=ctx.run_id or "",
            handoff_id=run["handoff_id"],
            objective=str(run["objective"] or ""),
            request_type=str(run["request_type"] or ""),
            release_record_id=ctx.release_record_id,
            candidate_git_sha=(evidence or {}).get("candidate_git_sha"),
        )
