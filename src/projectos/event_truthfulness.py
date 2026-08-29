"""Event truthfulness guards — domain events require real persisted evidence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from projectos.delivery.gates import GATE_STATUS_PASSED
from projectos.delivery.store import (
    get_delivery_release,
    list_delivery_artifacts,
    list_gate_statuses,
)
from projectos.errors import OrchestrationError
from projectos.store import get_job


def require_persisted_work(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    orchestration_job_id: int | None,
) -> None:
    row = conn.execute(
        "SELECT run_id, project_id, orchestration_job_id, status FROM remediation_work WHERE work_item_id = ?",
        (work_item_id,),
    ).fetchone()
    if row is None:
        raise OrchestrationError(
            f"AGENT_ASSIGNED requires persisted remediation work {work_item_id!r}"
        )
    if orchestration_job_id is not None:
        if row["orchestration_job_id"] != orchestration_job_id:
            raise OrchestrationError(
                f"AGENT_ASSIGNED references mismatched orchestration job {orchestration_job_id}"
            )
        job = conn.execute(
            "SELECT 1 FROM orchestration_jobs WHERE id = ?", (orchestration_job_id,)
        ).fetchone()
        if job is None:
            raise OrchestrationError(
                f"AGENT_ASSIGNED references missing orchestration job {orchestration_job_id}"
            )


def require_work_started_evidence(
    conn: sqlite3.Connection,
    *,
    ctx,
    evidence: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> None:
    work_item_id = (evidence or {}).get("work_item_id")
    job_id = (evidence or {}).get("orchestration_job_id") or (metadata or {}).get("job_id")
    if job_id is None and ctx and ctx.job_id:
        row = conn.execute(
            "SELECT id FROM orchestration_jobs WHERE human_id = ?",
            (ctx.job_id,),
        ).fetchone()
        job_id = row["id"] if row else None
    if not work_item_id and job_id is None:
        return
    if work_item_id:
        row = conn.execute(
            "SELECT status FROM remediation_work WHERE work_item_id = ?",
            (str(work_item_id),),
        ).fetchone()
        if row is None:
            raise OrchestrationError("WORK_STARTED requires persisted remediation work")
        if str(row["status"]) not in {"RUNNING", "READY"}:
            raise OrchestrationError("WORK_STARTED requires active remediation execution")
        return
    job = get_job(conn, int(job_id))
    if job.status not in {"LEASED", "RUNNING"}:
        raise OrchestrationError("WORK_STARTED requires claimed/running execution")
    if job.status == "LEASED":
        lease = conn.execute(
            """
            SELECT 1 FROM worker_leases
            WHERE job_id = ? AND released_at IS NULL
            """,
            (int(job_id),),
        ).fetchone()
        if lease is None:
            raise OrchestrationError("WORK_STARTED requires active worker lease")


def require_work_completion_evidence(evidence: dict[str, Any] | None) -> None:
    """Lightweight payload shape check before WORK_COMPLETED emission."""
    if not evidence or not evidence.get("work_item_id"):
        raise OrchestrationError("WORK_COMPLETED requires work_item_id evidence")
    if not (evidence.get("target_candidate_id") or evidence.get("candidate_git_sha")):
        raise OrchestrationError("WORK_COMPLETED requires candidate evidence")


def require_work_completed_authoritative(
    conn: sqlite3.Connection,
    *,
    ctx,
    evidence: dict[str, Any] | None,
) -> None:
    if not evidence or not evidence.get("work_item_id"):
        raise OrchestrationError("WORK_COMPLETED requires work_item_id evidence")
    work_item_id = str(evidence["work_item_id"])
    row = conn.execute(
        """
        SELECT run_id, project_id, orchestration_job_id, status
        FROM remediation_work
        WHERE work_item_id = ?
        """,
        (work_item_id,),
    ).fetchone()
    if row is None:
        raise OrchestrationError("WORK_COMPLETED requires persisted remediation work")
    if ctx.run_id and str(row["run_id"]) != str(ctx.run_id):
        raise OrchestrationError("WORK_COMPLETED run_id does not match remediation work")
    if str(row["status"]) not in {"SUCCEEDED", "COMPLETED"}:
        raise OrchestrationError("WORK_COMPLETED requires remediation work status COMPLETED")
    job_id = row["orchestration_job_id"]
    if job_id is None:
        raise OrchestrationError("WORK_COMPLETED requires linked orchestration job")
    job = get_job(conn, int(job_id))
    if job.status in {"FAILED", "BLOCKED", "CANCELLED"}:
        raise OrchestrationError("WORK_COMPLETED requires linked job not terminal-failed")
    candidate = evidence.get("target_candidate_id") or evidence.get("candidate_git_sha")
    if candidate and job.candidate_git_sha and str(job.candidate_git_sha) != str(candidate):
        raise OrchestrationError("WORK_COMPLETED candidate does not match job record")
    dup = conn.execute(
        """
        SELECT 1 FROM projectos_events
        WHERE event_type = 'WORK_COMPLETED'
          AND run_id = ?
          AND evidence_json LIKE ?
        LIMIT 1
        """,
        (ctx.run_id or row["run_id"], f'%"work_item_id": "{work_item_id}"%'),
    ).fetchone()
    if dup is not None:
        raise OrchestrationError("WORK_COMPLETED already emitted for work item")


def require_installer_artifact(path: str | Path) -> None:
    p = Path(path)
    if not p.is_file():
        raise OrchestrationError("INSTALLER_BUILT requires an existing installer artifact")
    if p.suffix.lower() == ".json" and "placeholder" in p.name.lower():
        raise OrchestrationError("INSTALLER_BUILT cannot reference placeholder artifact")


def require_qa_gate_evidence(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    candidate_git_sha: str | None,
    run_id: str | None,
    evidence: dict[str, Any] | None = None,
) -> None:
    from projectos.qa_gate import collect_qa_gate_facts

    candidate = candidate_git_sha or (evidence or {}).get("candidate_git_sha")
    resolved_run = run_id or (evidence or {}).get("run_id")
    if not candidate or not resolved_run:
        raise OrchestrationError("QA_GATE_PASSED requires run_id and candidate_git_sha")
    facts = collect_qa_gate_facts(
        conn,
        project_id=project_id,
        candidate_git_sha=str(candidate),
        run_id=str(resolved_run),
    )
    if str(facts.get("gate") or "") != "PASSED":
        raise OrchestrationError("QA_GATE_PASSED requires authoritative QA gate evidence")


def require_qa_manager_aggregated(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    candidate_git_sha: str | None,
    run_id: str | None,
    evidence: dict[str, Any] | None = None,
) -> None:
    candidate = candidate_git_sha or (evidence or {}).get("candidate_git_sha")
    resolved_run = run_id or (evidence or {}).get("run_id")
    if not candidate or not resolved_run:
        raise OrchestrationError("QA_MANAGER_AGGREGATED requires run_id and candidate_git_sha")
    from projectos.qa_gate import collect_qa_gate_facts

    facts = collect_qa_gate_facts(
        conn,
        project_id=project_id,
        candidate_git_sha=str(candidate),
        run_id=str(resolved_run),
    )
    if str(facts.get("gate") or "") not in {"PASSED", "FAILED", "INCONCLUSIVE"}:
        raise OrchestrationError("QA_MANAGER_AGGREGATED requires persisted assessor evidence")


def require_release_candidate(
    conn: sqlite3.Connection,
    *,
    release_record_id: str | None,
    candidate_git_sha: str | None,
) -> None:
    if not release_record_id:
        return
    record = get_delivery_release(conn, release_record_id=str(release_record_id))
    if record is None:
        raise OrchestrationError("RELEASE_CANDIDATE requires persisted release record")
    if candidate_git_sha and str(record.get("candidate_git_sha") or "") != str(candidate_git_sha):
        raise OrchestrationError("RELEASE_CANDIDATE candidate does not match release record")


def require_package_completed(
    conn: sqlite3.Connection,
    *,
    release_record_id: str | None,
    candidate_git_sha: str | None,
) -> None:
    if not release_record_id:
        return
    record = get_delivery_release(conn, release_record_id=str(release_record_id))
    if record is None:
        raise OrchestrationError("PACKAGE_COMPLETED requires persisted release record")
    artifacts = list_delivery_artifacts(conn, str(release_record_id))
    if not artifacts:
        raise OrchestrationError("PACKAGE_COMPLETED requires artifact records")
    if candidate_git_sha and str(record.get("candidate_git_sha") or "") != str(candidate_git_sha):
        raise OrchestrationError("PACKAGE_COMPLETED candidate mismatch")


def require_verify_gate_passed(conn: sqlite3.Connection, *, release_record_id: str) -> None:
    gates = list_gate_statuses(conn, release_record_id)
    if gates.get("VERIFY_GATE") != GATE_STATUS_PASSED:
        raise OrchestrationError("RELEASE_VERIFIED requires VERIFY_GATE PASSED evidence")


def require_publication_record(
    conn: sqlite3.Connection,
    evidence: dict[str, Any] | None,
    *,
    ctx=None,
) -> None:
    release_record_id = (evidence or {}).get("release_record_id") or (
        getattr(ctx, "release_record_id", None) if ctx else None
    )
    if not release_record_id:
        raise OrchestrationError("RELEASE_PUBLISHED requires release_record_id evidence")
    record = get_delivery_release(conn, release_record_id=str(release_record_id))
    if record is None:
        raise OrchestrationError("RELEASE_PUBLISHED requires persisted release record")
    if str(record.get("publication_status") or "") != "published":
        raise OrchestrationError("RELEASE_PUBLISHED requires publication_status=published")
    gates = list_gate_statuses(conn, str(release_record_id))
    if gates.get("PUBLICATION_GATE") != GATE_STATUS_PASSED:
        raise OrchestrationError("RELEASE_PUBLISHED requires PUBLICATION_GATE PASSED")
    url = str(record.get("github_release_url") or record.get("download_url") or "")
    if not url:
        raise OrchestrationError("RELEASE_PUBLISHED requires non-empty publication URL")


def require_sponsor_outcome_satisfied(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    handoff_id: str | None,
    objective: str,
    request_type: str | None,
    release_record_id: str | None,
    candidate_git_sha: str | None,
    project_id: str | None = None,
    registry_path: Path | str | None = None,
    repository_root: str | None = None,
) -> None:
    from projectos.sponsor_outcome import evaluate_sponsor_outcome

    if not candidate_git_sha and release_record_id:
        record = get_delivery_release(conn, release_record_id=str(release_record_id))
        if record is not None:
            candidate_git_sha = str(record.get("candidate_git_sha") or "") or None
    if not repository_root and project_id and run_id:
        from projectos.run_evidence import _repository_root_for_run

        repository_root = _repository_root_for_run(
            conn, run_id=run_id, project_id=project_id
        )

    evaluation = evaluate_sponsor_outcome(
        conn,
        run_id=run_id,
        handoff_id=handoff_id,
        objective=objective,
        request_type=request_type,
        release_record_id=release_record_id,
        candidate_git_sha=candidate_git_sha,
        project_id=project_id,
        registry_path=registry_path,
        repository_root=repository_root,
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
    registry_path: Path | str | None = None,
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
    elif event_type == "WORK_STARTED":
        require_work_started_evidence(conn, ctx=ctx, evidence=evidence, metadata=metadata)
    elif event_type == "WORK_COMPLETED":
        require_work_completed_authoritative(conn, ctx=ctx, evidence=evidence)
    elif event_type == "QA_GATE_PASSED":
        require_qa_gate_evidence(
            conn,
            project_id=ctx.project_id,
            candidate_git_sha=(evidence or {}).get("candidate_git_sha"),
            run_id=ctx.run_id,
            evidence=evidence,
        )
    elif event_type == "QA_MANAGER_AGGREGATED":
        require_qa_manager_aggregated(
            conn,
            project_id=ctx.project_id,
            candidate_git_sha=(evidence or {}).get("candidate_git_sha"),
            run_id=ctx.run_id,
            evidence=evidence,
        )
    elif event_type == "RELEASE_CANDIDATE":
        require_release_candidate(
            conn,
            release_record_id=(evidence or {}).get("release_record_id") or ctx.release_record_id,
            candidate_git_sha=(evidence or {}).get("candidate_git_sha"),
        )
    elif event_type == "PACKAGE_COMPLETED":
        require_package_completed(
            conn,
            release_record_id=(evidence or {}).get("release_record_id") or ctx.release_record_id,
            candidate_git_sha=(evidence or {}).get("candidate_git_sha"),
        )
    elif event_type == "RELEASE_PUBLISHED":
        require_publication_record(conn, evidence, ctx=ctx)
    elif event_type == "RELEASE_VERIFIED":
        release_record_id = (evidence or {}).get("release_record_id") or ctx.release_record_id
        if not release_record_id:
            raise OrchestrationError("RELEASE_VERIFIED requires release_record_id")
        require_verify_gate_passed(conn, release_record_id=str(release_record_id))
    elif event_type == "RUN_COMPLETED":
        run = conn.execute(
            "SELECT handoff_id, objective, request_type, project_id FROM execution_runs WHERE run_id = ?",
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
            project_id=str(run["project_id"]),
            registry_path=registry_path,
            repository_root=None,
        )
