"""Terminal ExecutionRun evidence assembled from authoritative ProjectOS state."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.candidate_model import get_run_active_candidate
from projectos.execution_run import get_execution_run, update_execution_run
from projectos.qa_gate import collect_qa_gate_facts
from projectos.run_outcomes import (
    EVENT_WAITING_FOR_SPONSOR,
    OUTCOME_CANCELLED_BY_SPONSOR,
    OUTCOME_MAX_REMEDIATION_EXCEEDED,
    OUTCOME_SPONSOR_DECISION_REQUIRED,
    OUTCOME_SUCCESS,
    OUTCOME_UNRECOVERABLE_TECHNICAL,
    TERMINAL_RUN_EVENTS,
    event_for_outcome,
    is_terminal_run_status,
    resolve_outcome,
    run_status_for_outcome,
)


def _handoff_desired_outputs(conn: sqlite3.Connection, handoff_id: str | None) -> dict[str, Any]:
    if not handoff_id:
        return {}
    row = conn.execute(
        "SELECT desired_outputs_json FROM sponsor_handoffs WHERE handoff_id = ?",
        (handoff_id,),
    ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(str(row["desired_outputs_json"] or "{}"))
    except json.JSONDecodeError:
        return {}


def _phases_completed(conn: sqlite3.Connection, run_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT phase FROM projectos_events
        WHERE run_id = ? AND phase IS NOT NULL AND phase != ''
        ORDER BY occurred_at ASC
        """,
        (run_id,),
    ).fetchall()
    return [str(r["phase"]) for r in rows if r["phase"]]


def _release_artifacts(conn: sqlite3.Connection, release_record_id: str | None) -> list[dict[str, Any]]:
    if not release_record_id:
        return []
    rows = conn.execute(
        """
        SELECT artifact_id, artifact_name, artifact_type, sha256, size_bytes,
               signature_status, local_build_path
        FROM delivery_artifacts
        WHERE release_record_id = ?
        ORDER BY created_at ASC
        """,
        (release_record_id,),
    ).fetchall()
    artifacts = []
    for row in rows:
        entry: dict[str, Any] = {
            "artifact_id": row["artifact_id"],
            "filename": row["artifact_name"],
            "artifact_type": row["artifact_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "signature_status": row["signature_status"],
        }
        if row["artifact_type"] == "sbom":
            entry["sbom"] = row["artifact_name"]
        artifacts.append(entry)
    return artifacts


def build_terminal_evidence(conn: sqlite3.Connection, *, run_id: str) -> dict[str, Any]:
    """Reconstruct terminal evidence from authoritative IDs and state."""
    run = get_execution_run(conn, run_id)
    if run is None:
        return {}

    release_id = None
    release_record_id = None
    publication_url = None
    row = conn.execute(
        """
        SELECT release_id, release_record_id, artifact_id, metadata_json
        FROM projectos_events
        WHERE run_id = ? AND (release_id IS NOT NULL OR release_record_id IS NOT NULL)
        ORDER BY occurred_at DESC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if row:
        release_id = row["release_id"]
        release_record_id = row["release_record_id"]
        if not release_record_id and row["metadata_json"]:
            try:
                meta = json.loads(str(row["metadata_json"]))
                release_record_id = meta.get("release_record_id")
            except json.JSONDecodeError:
                pass
        if not release_record_id:
            release_record_id = row["artifact_id"]

    if release_record_id:
        rel = conn.execute(
            "SELECT github_release_url FROM delivery_releases WHERE release_record_id = ?",
            (release_record_id,),
        ).fetchone()
        if rel and rel["github_release_url"]:
            publication_url = str(rel["github_release_url"])

    jobs = [
        dict(r)
        for r in conn.execute(
            """
            SELECT human_id, queue, status FROM orchestration_jobs
            WHERE project_human_id = ?
            ORDER BY updated_at DESC LIMIT 20
            """,
            (run.project_id,),
        ).fetchall()
    ]

    failure: dict[str, Any] | None = None
    if run.status in {"BLOCKED", "FAILED", "ESCALATED", "CANCELLED"}:
        fail_evt = conn.execute(
            """
            SELECT phase, summary, evidence_json FROM projectos_events
            WHERE run_id = ? AND event_type IN (
                'RUN_FAILED', 'RUN_BLOCKED', 'RUN_ESCALATED', 'RUN_CANCELLED',
                'QA_GATE_FAILED',
                'PACKAGE_FAILED', 'RELEASE_BLOCKED', 'DELIVERY_BLOCKED',
                'RELEASE_PREPARATION_BLOCKED', 'WORK_FAILED'
            )
            ORDER BY occurred_at DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if fail_evt:
            evidence = {}
            if fail_evt["evidence_json"]:
                try:
                    evidence = json.loads(str(fail_evt["evidence_json"]))
                except json.JSONDecodeError:
                    evidence = {}
            nested_failure = evidence.get("failure") if isinstance(evidence.get("failure"), dict) else None
            if nested_failure and nested_failure.get("blocker_type"):
                failure = nested_failure
            elif evidence.get("blocker_type"):
                failure = dict(evidence)
                failure.setdefault("reason", fail_evt["summary"])
            elif nested_failure:
                failure = nested_failure
            else:
                failure = {
                    "phase": fail_evt["phase"],
                    "reason": fail_evt["summary"],
                    "retryable": evidence.get("retryable", True),
                    "required_action": evidence.get("required_action") or evidence.get("sponsor_impact"),
                }
                if evidence:
                    failure["detail"] = evidence

    artifacts = _release_artifacts(conn, release_record_id)
    if publication_url and artifacts:
        for art in artifacts:
            if art.get("artifact_type") == "installer":
                art["publication_url"] = publication_url

    active_candidate = get_run_active_candidate(conn, run_id)

    return {
        "run_id": run.run_id,
        "handoff_id": run.handoff_id,
        "project_id": run.project_id,
        "request_type": run.request_type,
        "objective": run.objective,
        "terminal_status": run.status,
        "result_summary": run.result_summary,
        "phases_completed": _phases_completed(conn, run_id),
        "work_items": [],
        "jobs": jobs,
        "release_id": release_id,
        "release_record_id": release_record_id,
        "artifacts": artifacts,
        "qa": collect_qa_gate_facts(
            conn,
            project_id=run.project_id,
            run_id=run_id,
            candidate_git_sha=active_candidate,
        ),
        "failure": failure,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def _run_already_terminal(conn: sqlite3.Connection, run_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM projectos_events
        WHERE run_id = ? AND event_type IN (
            'RUN_COMPLETED', 'RUN_BLOCKED', 'RUN_FAILED',
            'RUN_ESCALATED', 'RUN_CANCELLED'
        )
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    return row is not None


def pause_run_for_sponsor_decision(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    summary: str,
    detail: str = "",
    evidence: dict[str, Any] | None = None,
) -> None:
    """Non-terminal pause: SPONSOR DECISION REQUIRED → WAITING_FOR_SPONSOR."""
    run_id = event_ctx.run_id
    if not run_id:
        return
    update_execution_run(
        conn,
        run_id=run_id,
        status=run_status_for_outcome(OUTCOME_SPONSOR_DECISION_REQUIRED),
        current_phase="sponsor_decision",
        current_agent=ACTOR_PM,
        progress=40,
        result_summary=summary[:4000],
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type=EVENT_WAITING_FOR_SPONSOR,
        summary=summary,
        actor_id=ACTOR_PM,
        phase="sponsor_decision",
        status=run_status_for_outcome(OUTCOME_SPONSOR_DECISION_REQUIRED),
        detail=detail[:2000],
        detail_level="milestone",
        evidence=evidence,
        subscribers=("slack",),
    )


def close_execution_run(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    summary: str,
    detail: str = "",
    failure: dict[str, Any] | None = None,
    outcome: str | None = None,
    terminal_status: str | None = None,
) -> dict[str, Any]:
    """PM authority: close an ExecutionRun with terminal evidence.

    Prefer `outcome` (canonical taxonomy). `terminal_status` is a legacy alias.
    """
    run_id = event_ctx.run_id
    if not run_id:
        return {}
    run = get_execution_run(conn, run_id)
    if run is None:
        return {}
    if _run_already_terminal(conn, run_id):
        return build_terminal_evidence(conn, run_id=run_id)

    if outcome is None:
        legacy_map = {
            "COMPLETED": OUTCOME_SUCCESS,
            "BLOCKED": OUTCOME_UNRECOVERABLE_TECHNICAL,
            "FAILED": OUTCOME_UNRECOVERABLE_TECHNICAL,
            "CANCELLED": OUTCOME_CANCELLED_BY_SPONSOR,
            "ESCALATED": OUTCOME_MAX_REMEDIATION_EXCEEDED,
        }
        outcome = legacy_map.get(str(terminal_status or "").upper(), OUTCOME_UNRECOVERABLE_TECHNICAL)
    resolved = resolve_outcome(outcome)
    run_status = run_status_for_outcome(resolved)
    event_type = event_for_outcome(resolved)

    update_execution_run(
        conn,
        run_id=run_id,
        status=run_status,
        current_phase="terminal",
        current_agent=ACTOR_PM,
        progress=100 if resolved == OUTCOME_SUCCESS else 90,
        result_summary=summary[:4000],
    )
    evidence = build_terminal_evidence(conn, run_id=run_id)
    evidence["outcome"] = resolved
    if failure:
        evidence["failure"] = failure

    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type=event_type,
        summary=summary,
        actor_id=ACTOR_PM,
        phase="terminal",
        status=run_status,
        detail=detail[:2000],
        detail_level="milestone",
        evidence=evidence,
        subscribers=("slack",),
    )
    update_execution_run(conn, run_id=run_id, evidence=evidence)
    return evidence


def requires_production_installer(conn: sqlite3.Connection, *, handoff_id: str | None, objective: str) -> bool:
    desired = _handoff_desired_outputs(conn, handoff_id)
    if desired.get("installer") or desired.get("download_link") or desired.get("publish"):
        return True
    lowered = objective.lower()
    return any(w in lowered for w in ("installer", "download", "publish", "release"))


def maybe_close_run_after_event(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    event_type: str,
) -> None:
    """PM observes downstream events — only PM terminal paths close runs here.

    Operational evidence events (QA_GATE_FAILED, PACKAGE_FAILED, etc.) are
  nonterminal and handled by PM remediation policy elsewhere.
    """
    run_id = event_ctx.run_id
    if not run_id:
        return
    if get_execution_run(conn, run_id) is None:
        return
    if _run_already_terminal(conn, run_id):
        return

    if event_type == "RELEASE_PUBLISHED":
        run = get_execution_run(conn, run_id)
        if run is None:
            return
        from projectos.sponsor_outcome import evaluate_sponsor_outcome

        release_record_id = release_record_id_from_event(conn, run_id)
        evaluation = evaluate_sponsor_outcome(
            conn,
            run_id=run_id,
            handoff_id=run.handoff_id,
            objective=run.objective or "",
            request_type=run.request_type,
            release_record_id=release_record_id,
            candidate_git_sha=active_candidate_sha(conn, run_id),
        )
        if not evaluation.satisfied:
            emit_projectos_event(
                conn,
                ctx=event_ctx,
                event_type="SPONSOR_OUTCOME_INCOMPLETE",
                summary="Release published but sponsor-requested outcomes are not satisfied.",
                actor_id=ACTOR_PM,
                phase="terminal",
                detail_level="milestone",
                evidence={
                    "required_outputs": evaluation.required_outputs,
                    "missing_outputs": evaluation.missing_outputs,
                    "satisfied_outputs": evaluation.satisfied_outputs,
                },
            )
            return
        close_execution_run(
            conn,
            event_ctx=EventContext(
                project_id=event_ctx.project_id,
                handoff_id=event_ctx.handoff_id,
                run_id=event_ctx.run_id,
                release_record_id=release_record_id,
            ),
            outcome=OUTCOME_SUCCESS,
            summary="Sponsor-requested outcomes verified.",
        )


def release_record_id_from_event(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT release_record_id, evidence_json, event_type
        FROM projectos_events
        WHERE run_id = ?
          AND (
            release_record_id IS NOT NULL
            OR event_type IN (
                'RELEASE_PUBLISHED', 'RELEASE_PREPARED', 'PACKAGE_COMPLETED',
                'RELEASE_VERIFIED', 'SOURCE_GATE_PASSED'
            )
          )
        ORDER BY occurred_at DESC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if not row:
        return None
    if row["release_record_id"]:
        return str(row["release_record_id"])
    if row["evidence_json"]:
        try:
            evidence = json.loads(str(row["evidence_json"]))
            rid = evidence.get("release_record_id")
            if rid:
                return str(rid)
        except json.JSONDecodeError:
            pass
    return None


def active_candidate_sha(conn: sqlite3.Connection, run_id: str) -> str | None:
    return get_run_active_candidate(conn, run_id)
