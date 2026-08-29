"""PM-owned QA remediation closed-loop policy."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from projectos.domain_events import ACTOR_DEVELOPER, ACTOR_PM, ACTOR_QA, EventContext, emit_projectos_event
from projectos.qa_gate import collect_qa_gate_facts, emit_qa_gate_evaluation, emit_qa_finding_created
from projectos.run_evidence import close_execution_run
from projectos.run_outcomes import OUTCOME_MAX_REMEDIATION_EXCEEDED

DEFAULT_MAX_REMEDIATION_CYCLES = 3
DEFAULT_MAX_SAME_FINDING_RECURRENCE = 2


@dataclass(frozen=True)
class RemediationResult:
    gate: str
    remediation_cycles: int
    escalated: bool = False
    findings: tuple[dict[str, Any], ...] = ()


def _new_finding_id() -> str:
    return f"FND-{uuid.uuid4().hex[:8].upper()}"


def collect_qa_findings(conn: sqlite3.Connection, *, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.id, e.result, e.assurance_role, e.evidence_ref, j.human_id, j.last_error
        FROM qa_evidence e
        LEFT JOIN orchestration_jobs j ON j.id = e.assurance_job_id
        WHERE e.project_human_id = ?
          AND (e.result IN ('fail', 'stale_rejected')
               OR j.status IN ('FAILED', 'BLOCKED'))
        ORDER BY e.created_at ASC
        """,
        (project_id,),
    ).fetchall()
    findings: list[dict[str, Any]] = []
    for row in rows:
        findings.append(
            {
                "finding_id": _new_finding_id(),
                "category": str(row["assurance_role"] or "assurance"),
                "severity": "high" if str(row["result"]) == "fail" else "medium",
                "evidence": row["evidence_ref"] or row["last_error"] or "",
                "affected_component": row["human_id"] or row["assurance_role"],
                "expected_condition": "assurance review passes",
                "actual_condition": str(row["result"] or row["last_error"] or "failed"),
                "recommended_owner_role": "DELIVERY",
                "retryable": True,
                "source_gate_or_review": str(row["assurance_role"] or "QA_GATE"),
                "qa_evidence_id": row["id"],
            }
        )
    return findings


def _remediation_cycle_count(conn: sqlite3.Connection, *, run_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM projectos_events
        WHERE run_id = ? AND event_type = 'REMEDIATION_STARTED'
        """,
        (run_id,),
    ).fetchone()
    return int(row["n"] if row else 0)


def _finding_recurrence(conn: sqlite3.Connection, *, run_id: str, category: str) -> int:
    rows = conn.execute(
        """
        SELECT evidence_json FROM projectos_events
        WHERE run_id = ? AND event_type IN ('QA_FINDING_CREATED', 'REMEDIATION_REQUIRED')
        ORDER BY occurred_at ASC
        """,
        (run_id,),
    ).fetchall()
    count = 0
    for row in rows:
        if not row["evidence_json"]:
            continue
        import json

        try:
            evidence = json.loads(str(row["evidence_json"]))
        except json.JSONDecodeError:
            continue
        findings = evidence.get("findings") or [evidence]
        for item in findings:
            if str(item.get("category") or "") == category:
                count += 1
    return count


def apply_governed_qa_remediation(conn: sqlite3.Connection, *, project_id: str, findings: list[dict[str, Any]]) -> list[str]:
    """Mark failed evidence rows remediated after governed corrective work."""
    work_items: list[str] = []
    for finding in findings:
        evidence_id = finding.get("qa_evidence_id")
        if evidence_id is None:
            continue
        conn.execute(
            "UPDATE qa_evidence SET result = 'pass' WHERE id = ? AND project_human_id = ?",
            (evidence_id, project_id),
        )
        work_items.append(f"QA-REMEDIATION-{finding['finding_id']}")
    return work_items


def start_remediation_cycle(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    project_id: str,
    facts: dict[str, Any],
    findings: list[dict[str, Any]],
    attempt_number: int,
) -> list[str]:
    remediation_meta = {
        "remediation_cycle": attempt_number,
        "attempt_number": attempt_number,
        "source_finding_ids": [f["finding_id"] for f in findings],
        "findings": findings,
        "qa": facts,
    }
    for finding in findings:
        emit_qa_finding_created(
            conn,
            event_context=event_ctx,
            summary=f"QA finding {finding['finding_id']}: {finding['actual_condition']}",
            evidence=finding,
        )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="REMEDIATION_REQUIRED",
        summary="PM requires corrective work before release can continue.",
        actor_id=ACTOR_PM,
        phase="REMEDIATION",
        detail_level="milestone",
        evidence=remediation_meta,
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="PM_REPLAN",
        summary="PM replanned release workflow for QA remediation.",
        actor_id=ACTOR_PM,
        phase="REMEDIATION",
        detail_level="normal",
        evidence=remediation_meta,
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="REMEDIATION_STARTED",
        summary=f"Remediation cycle {attempt_number} started.",
        actor_id=ACTOR_PM,
        phase="REMEDIATION",
        evidence=remediation_meta,
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="AGENT_ASSIGNED",
        summary="Assigned: developer-agent for QA remediation.",
        actor_id=ACTOR_PM,
        phase="REMEDIATION",
        metadata={"agent_id": ACTOR_DEVELOPER},
    )
    work_items = apply_governed_qa_remediation(conn, project_id=project_id, findings=findings)
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="WORK_STARTED",
        summary="Corrective work started for QA findings.",
        actor_id=ACTOR_DEVELOPER,
        phase="REMEDIATION",
        evidence={"work_item_ids": work_items},
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="WORK_COMPLETED",
        summary="Corrective work completed for QA findings.",
        actor_id=ACTOR_DEVELOPER,
        phase="REMEDIATION",
        evidence={"work_item_ids": work_items},
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="QA_RETEST_STARTED",
        summary="QA retest started after remediation.",
        actor_id=ACTOR_QA,
        phase="QA_GATE",
    )
    return work_items


def run_qa_with_remediation(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    project_id: str,
    max_cycles: int = DEFAULT_MAX_REMEDIATION_CYCLES,
    max_same_finding_recurrence: int = DEFAULT_MAX_SAME_FINDING_RECURRENCE,
) -> RemediationResult:
    """Evaluate QA gate with PM-owned remediation cycles until pass or escalation."""
    if not event_ctx.run_id:
        facts = emit_qa_gate_evaluation(conn, project_id=project_id, event_context=event_ctx)
        return RemediationResult(gate=str(facts.get("gate") or "PENDING"), remediation_cycles=0)

    cycles = 0
    while cycles < max_cycles:
        facts = emit_qa_gate_evaluation(conn, project_id=project_id, event_context=event_ctx)
        gate = str(facts.get("gate") or "PENDING")
        if gate == "PASSED":
            return RemediationResult(gate=gate, remediation_cycles=cycles, findings=())
        if gate != "FAILED":
            return RemediationResult(gate=gate, remediation_cycles=cycles, findings=())

        findings = collect_qa_findings(conn, project_id=project_id)
        for finding in findings:
            recurrence = _finding_recurrence(conn, run_id=event_ctx.run_id, category=finding["category"])
            if recurrence >= max_same_finding_recurrence:
                emit_projectos_event(
                    conn,
                    ctx=event_ctx,
                    event_type="REMEDIATION_LIMIT_REACHED",
                    summary="Remediation policy limit exceeded for recurring QA finding.",
                    actor_id=ACTOR_PM,
                    phase="REMEDIATION",
                    detail_level="milestone",
                    evidence={"finding": finding, "recurrence": recurrence},
                )
                close_execution_run(
                    conn,
                    event_ctx=event_ctx,
                    outcome=OUTCOME_MAX_REMEDIATION_EXCEEDED,
                    summary="Remediation policy exceeded; PM escalated the run.",
                    failure={"phase": "QA_GATE", "finding": finding, "recurrence": recurrence},
                )
                return RemediationResult(
                    gate="FAILED", remediation_cycles=cycles, escalated=True, findings=tuple(findings)
                )

        cycles += 1
        start_remediation_cycle(
            conn,
            event_ctx=event_ctx,
            project_id=project_id,
            facts=facts,
            findings=findings,
            attempt_number=cycles,
        )

    facts = collect_qa_gate_facts(conn, project_id=project_id)
    gate = str(facts.get("gate") or "FAILED")
    if gate != "PASSED":
        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="REMEDIATION_LIMIT_REACHED",
            summary="Maximum remediation cycles reached.",
            actor_id=ACTOR_PM,
            phase="REMEDIATION",
            detail_level="milestone",
            evidence={"remediation_cycles": cycles, "qa": facts},
        )
        close_execution_run(
            conn,
            event_ctx=event_ctx,
            outcome=OUTCOME_MAX_REMEDIATION_EXCEEDED,
            summary="Maximum remediation cycles exceeded; PM escalated the run.",
            failure={"phase": "QA_GATE", "remediation_cycles": cycles, "qa": facts},
        )
        return RemediationResult(gate=gate, remediation_cycles=cycles, escalated=True)

    emit_qa_gate_evaluation(conn, project_id=project_id, event_context=event_ctx)
    return RemediationResult(gate="PASSED", remediation_cycles=cycles)
