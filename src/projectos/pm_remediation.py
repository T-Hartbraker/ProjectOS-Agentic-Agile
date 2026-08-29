"""PM-owned QA remediation closed-loop policy with real executable work."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from projectos.candidate_model import (
    CANDIDATE_TYPE_GIT_SHA,
    latest_candidate_sha,
    set_run_active_candidate,
)
from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.finding_fingerprint import durable_finding_id, finding_fingerprint
from projectos.finding_routing import classify_finding_category
from projectos.qa_gate import collect_qa_gate_facts, emit_qa_gate_evaluation, emit_qa_finding_created
from projectos.qa_retest import AssuranceExecutor, execute_qa_retest
from projectos.remediation_capability import group_findings_for_remediation, resolve_remediation_execution
from projectos.remediation_executor import RemediationExecutionResult, RemediationWorker, execute_remediation_work
from projectos.remediation_store import count_remediation_cycles, create_remediation_work
from projectos.run_evidence import close_execution_run
from projectos.run_liveness import assert_run_has_next_action
from projectos.run_outcomes import OUTCOME_MAX_REMEDIATION_EXCEEDED

DEFAULT_MAX_REMEDIATION_CYCLES = 3
DEFAULT_MAX_SAME_FINDING_RECURRENCE = 2


@dataclass(frozen=True)
class RemediationResult:
    gate: str
    remediation_cycles: int
    escalated: bool = False
    findings: tuple[dict[str, Any], ...] = ()


def _build_finding(row) -> dict[str, Any]:
    assurance_role = str(row["assurance_role"] or "assurance")
    actual = str(row["result"] or row["last_error"] or "failed")
    category = classify_finding_category(assurance_role=assurance_role, actual_condition=actual)
    base = {
        "category": category,
        "severity": "high" if str(row["result"]) == "fail" else "medium",
        "evidence": row["evidence_ref"] or row["last_error"] or "",
        "affected_component": row["human_id"] or assurance_role,
        "expected_condition": "assurance review passes",
        "actual_condition": actual,
        "recommended_owner_role": category,
        "retryable": True,
        "source_gate_or_review": assurance_role,
        "qa_evidence_id": row["id"],
        "candidate_git_sha": row["candidate_git_sha"],
    }
    fp = finding_fingerprint(base)
    base["fingerprint"] = fp
    base["finding_id"] = durable_finding_id(base, qa_evidence_id=int(row["id"]))
    return base


def collect_qa_findings(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    candidate_git_sha: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["e.project_human_id = ?"]
    params: list[Any] = [project_id]
    if candidate_git_sha:
        clauses.append("e.candidate_git_sha = ?")
        params.append(candidate_git_sha)
    if run_id:
        clauses.append("e.run_id = ?")
        params.append(run_id)
    rows = conn.execute(
        f"""
        SELECT e.id, e.result, e.assurance_role, e.evidence_ref, e.candidate_git_sha,
               j.human_id, j.last_error
        FROM qa_evidence e
        LEFT JOIN orchestration_jobs j ON j.id = e.assurance_job_id
        WHERE {' AND '.join(clauses)}
          AND (e.result IN ('fail', 'stale_rejected')
               OR j.status IN ('FAILED', 'BLOCKED'))
        ORDER BY e.created_at ASC
        """,
        params,
    ).fetchall()
    findings: list[dict[str, Any]] = []
    for row in rows:
        findings.append(_build_finding(row))
    return findings


def _finding_recurrence(conn: sqlite3.Connection, *, run_id: str, fingerprint: str) -> int:
    """Count remediation cycles that addressed the same finding fingerprint."""
    import json

    rows = conn.execute(
        """
        SELECT evidence_json FROM projectos_events
        WHERE run_id = ? AND event_type = 'REMEDIATION_STARTED'
        ORDER BY occurred_at ASC
        """,
        (run_id,),
    ).fetchall()
    count = 0
    for row in rows:
        if not row["evidence_json"]:
            continue
        try:
            evidence = json.loads(str(row["evidence_json"]))
        except json.JSONDecodeError:
            continue
        for item in evidence.get("findings") or []:
            if str(item.get("fingerprint") or "") == fingerprint:
                count += 1
                break
    return count


def start_remediation_cycle(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    project_id: str,
    facts: dict[str, Any],
    findings: list[dict[str, Any]],
    attempt_number: int,
    repository_root: str,
    worker: RemediationWorker | None = None,
    service_ctx=None,
    assurance_executor: AssuranceExecutor | None = None,
) -> RemediationExecutionResult:
    source_candidate = latest_candidate_sha(conn, project_id=project_id, run_id=event_ctx.run_id)
    remediation_meta = {
        "remediation_cycle": attempt_number,
        "attempt_number": attempt_number,
        "source_finding_ids": [f["finding_id"] for f in findings],
        "findings": findings,
        "qa": facts,
        "source_candidate_id": source_candidate,
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

    groups = group_findings_for_remediation(findings)
    last_outcome: RemediationExecutionResult | None = None
    for assigned_agent, execution_queue, group_findings, reason in groups:
        owner, _, _, _ = resolve_remediation_execution(group_findings[0])
        work = create_remediation_work(
            conn,
            run_id=event_ctx.run_id or project_id,
            project_id=project_id,
            remediation_cycle=attempt_number,
            finding_ids=[f["finding_id"] for f in group_findings],
            assigned_agent=assigned_agent,
            objective=f"Remediate QA findings for candidate {source_candidate}",
            acceptance_criteria="Produce new candidate and pass independent QA retest.",
            source_candidate_id=source_candidate,
            repository_root=repository_root,
            assignment_reason=reason,
            findings=group_findings,
            execution_queue=execution_queue,
            finding_owner=owner,
        )
        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="AGENT_ASSIGNED",
            summary=f"Assigned: {assigned_agent} for QA remediation.",
            actor_id=ACTOR_PM,
            phase="REMEDIATION",
            metadata={
                "agent_id": assigned_agent,
                "finding_owner": owner,
                "execution_queue": execution_queue,
                "finding_ids": [f["finding_id"] for f in group_findings],
                "reason": reason,
                "remediation_cycle": attempt_number,
                "work_item_id": work.work_item_id,
            },
            evidence={"work_item_id": work.work_item_id, "orchestration_job_id": work.orchestration_job_id},
        )
        last_outcome = execute_remediation_work(
            conn,
            work=work,
            event_ctx=event_ctx,
            project_id=project_id,
            repository_root=repository_root,
            worker=worker,
            service_ctx=service_ctx,
        )
        if last_outcome.status != "COMPLETED" or not last_outcome.target_candidate_id:
            return last_outcome

    outcome = last_outcome
    if outcome is None or outcome.target_candidate_id is None:
        return RemediationExecutionResult(
            work_item_id="",
            status="FAILED",
            target_candidate_id=None,
            evidence={"reason": "no_remediation_groups"},
        )

    roles = [str(f.get("source_gate_or_review") or "") for f in findings if f.get("source_gate_or_review")]
    retest = execute_qa_retest(
        conn,
        event_ctx=event_ctx,
        project_id=project_id,
        repository_root=repository_root,
        candidate_id=outcome.target_candidate_id,
        candidate_type=outcome.candidate_type,
        run_id=event_ctx.run_id,
        remediation_cycle=attempt_number,
        retest_roles=roles or None,
        source_remediation_job_id=work.orchestration_job_id,
        source_candidate_id=source_candidate,
        service_ctx=service_ctx,
        assurance_executor=assurance_executor,
    )
    set_run_active_candidate(
        conn,
        run_id=event_ctx.run_id or project_id,
        candidate_id=retest.candidate_id,
        candidate_type=retest.candidate_type,
        remediation_cycle=attempt_number,
    )
    if retest.gate == "PASSED":
        emit_qa_gate_evaluation(
            conn,
            project_id=project_id,
            event_context=event_ctx,
            candidate_git_sha=retest.candidate_id,
            run_id=event_ctx.run_id,
        )
    elif retest.gate == "FAILED":
        emit_qa_gate_evaluation(
            conn,
            project_id=project_id,
            event_context=event_ctx,
            candidate_git_sha=retest.candidate_id,
            run_id=event_ctx.run_id,
        )
    return outcome


def run_qa_with_remediation(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    project_id: str,
    repository_root: str = "/repo",
    max_cycles: int = DEFAULT_MAX_REMEDIATION_CYCLES,
    max_same_finding_recurrence: int = DEFAULT_MAX_SAME_FINDING_RECURRENCE,
    worker: RemediationWorker | None = None,
    service_ctx=None,
    assurance_executor: AssuranceExecutor | None = None,
) -> RemediationResult:
    """Evaluate QA gate with PM-owned remediation cycles until pass or escalation."""
    from projectos.orchestration_boundary import run_with_internal_defect_routing

    return run_with_internal_defect_routing(
        conn,
        event_ctx=event_ctx,
        project_id=project_id,
        component="pm_remediation",
        operation="run_qa_with_remediation",
        in_project_scope=True,
        service_ctx=service_ctx,
        worker=worker,
        repository_root=repository_root,
        fn=lambda: _run_qa_with_remediation_impl(
            conn,
            event_ctx=event_ctx,
            project_id=project_id,
            repository_root=repository_root,
            max_cycles=max_cycles,
            max_same_finding_recurrence=max_same_finding_recurrence,
            worker=worker,
            service_ctx=service_ctx,
            assurance_executor=assurance_executor,
        ),
    )


def _run_qa_with_remediation_impl(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    project_id: str,
    repository_root: str = "/repo",
    max_cycles: int = DEFAULT_MAX_REMEDIATION_CYCLES,
    max_same_finding_recurrence: int = DEFAULT_MAX_SAME_FINDING_RECURRENCE,
    worker: RemediationWorker | None = None,
    service_ctx=None,
    assurance_executor: AssuranceExecutor | None = None,
) -> RemediationResult:
    """Internal QA remediation loop — wrapped by orchestration boundary."""
    if not event_ctx.run_id:
        facts = emit_qa_gate_evaluation(conn, project_id=project_id, event_context=event_ctx)
        return RemediationResult(gate=str(facts.get("gate") or "PENDING"), remediation_cycles=0)

    while True:
        cycles = count_remediation_cycles(conn, run_id=event_ctx.run_id)
        if cycles >= max_cycles:
            break
        candidate = latest_candidate_sha(conn, project_id=project_id, run_id=event_ctx.run_id)
        facts = emit_qa_gate_evaluation(
            conn,
            project_id=project_id,
            event_context=event_ctx,
            candidate_git_sha=candidate,
            run_id=event_ctx.run_id,
        )
        gate = str(facts.get("gate") or "PENDING")
        if gate == "PASSED":
            return RemediationResult(gate=gate, remediation_cycles=cycles, findings=())
        if gate == "INCONCLUSIVE":
            from projectos.qa_inconclusive import schedule_assurance_retry_for_inconclusive

            schedule_assurance_retry_for_inconclusive(
                conn,
                event_ctx=event_ctx,
                project_id=project_id,
                repository_root=repository_root,
                candidate_git_sha=candidate or "",
                run_id=event_ctx.run_id or "",
            )
            assert_run_has_next_action(
                conn, run_id=event_ctx.run_id, project_id=project_id
            )
            return RemediationResult(gate=gate, remediation_cycles=cycles, findings=())
        if gate != "FAILED":
            return RemediationResult(gate=gate, remediation_cycles=cycles, findings=())

        findings = collect_qa_findings(
            conn,
            project_id=project_id,
            candidate_git_sha=candidate,
            run_id=event_ctx.run_id,
        )
        for finding in findings:
            recurrence = _finding_recurrence(
                conn, run_id=event_ctx.run_id, fingerprint=finding["fingerprint"]
            )
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

        start_remediation_cycle(
            conn,
            event_ctx=event_ctx,
            project_id=project_id,
            facts=facts,
            findings=findings,
            attempt_number=cycles + 1,
            repository_root=repository_root,
            worker=worker,
            service_ctx=service_ctx,
            assurance_executor=assurance_executor,
        )

    candidate = latest_candidate_sha(conn, project_id=project_id, run_id=event_ctx.run_id)
    facts = collect_qa_gate_facts(
        conn, project_id=project_id, candidate_git_sha=candidate, run_id=event_ctx.run_id
    )
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
