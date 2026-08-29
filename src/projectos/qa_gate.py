"""Authoritative QA / assurance gate evaluation and transactional events."""

from __future__ import annotations

import sqlite3
from typing import Any

from projectos.domain_events import ACTOR_QA, EventContext, emit_projectos_event


def collect_qa_gate_facts(conn: sqlite3.Connection, *, project_id: str) -> dict[str, Any]:
    """Typed QA facts from authoritative qa_evidence and assurance jobs only."""
    rows = conn.execute(
        """
        SELECT e.result, e.assurance_role, j.status AS job_status, j.human_id
        FROM qa_evidence e
        LEFT JOIN orchestration_jobs j ON j.id = e.assurance_job_id
        WHERE e.project_human_id = ?
        ORDER BY e.created_at DESC
        """,
        (project_id,),
    ).fetchall()
    reviews_total = len(rows)
    passed = [r for r in rows if str(r["result"]) == "pass"]
    failed = [
        r
        for r in rows
        if str(r["result"]) in {"fail", "stale_rejected"}
        or str(r["job_status"] or "") in {"FAILED", "BLOCKED"}
    ]
    pending = [
        r
        for r in rows
        if str(r["result"]) == "pending"
        or str(r["job_status"] or "") in {"READY", "QUEUED", "LEASED", "RUNNING"}
    ]
    if failed:
        gate = "FAILED"
    elif reviews_total and not pending and len(passed) == reviews_total:
        gate = "PASSED"
    elif pending:
        gate = "PENDING"
    else:
        gate = "PASSED" if reviews_total == 0 else "PENDING"

    facts: dict[str, Any] = {
        "reviews_total": reviews_total if reviews_total else None,
        "reviews_completed": len(passed) if reviews_total else None,
        "reviews_need_attention": len(failed) if reviews_total else None,
        "reviews_pending": len(pending) if reviews_total else None,
        "gate": gate,
        "tests_total": None,
        "tests_passed": None,
        "tests_failed": None,
        "tests_skipped": None,
    }
    return facts


def emit_qa_gate_evaluation(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    event_context: EventContext | None,
    require_pass: bool = False,
) -> dict[str, Any]:
    """Evaluate QA gate and emit transactional ProjectOS events at the mutation boundary."""
    if event_context is None:
        return collect_qa_gate_facts(conn, project_id=project_id)

    run_key = event_context.run_id or project_id
    facts = collect_qa_gate_facts(conn, project_id=project_id)
    started = conn.execute(
        """
        SELECT 1 FROM projectos_events
        WHERE run_id = ? AND event_type = 'QA_STARTED'
        LIMIT 1
        """,
        (event_context.run_id,),
    ).fetchone()
    if event_context.run_id and not started:
        emit_projectos_event(
            conn,
            ctx=event_context,
            event_type="QA_STARTED",
            summary="Validation started.",
            actor_id=ACTOR_QA,
            phase="QA_GATE",
            detail_level="normal",
            evidence=facts,
        )

    gate = str(facts.get("gate") or "PENDING")
    if gate == "PASSED":
        emit_projectos_event(
            conn,
            ctx=event_context,
            event_type="QA_GATE_PASSED",
            summary="QA gate passed.",
            actor_id=ACTOR_QA,
            phase="QA_GATE",
            status="PASSED",
            detail_level="milestone",
            evidence=facts,
        )
    elif gate == "FAILED":
        emit_projectos_event(
            conn,
            ctx=event_context,
            event_type="QA_GATE_FAILED",
            summary="QA gate failed.",
            actor_id=ACTOR_QA,
            phase="QA_GATE",
            status="FAILED",
            detail_level="milestone",
            evidence=facts,
        )
        if require_pass:
            from projectos.errors import OrchestrationError

            raise OrchestrationError("QA gate failed; release cannot proceed")
    return facts


def emit_qa_finding_created(
    conn: sqlite3.Connection,
    *,
    event_context: EventContext,
    summary: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    emit_projectos_event(
        conn,
        ctx=event_context,
        event_type="QA_FINDING_CREATED",
        summary=summary[:500],
        actor_id=ACTOR_QA,
        phase="QA_GATE",
        detail_level="milestone",
        evidence=evidence,
    )
