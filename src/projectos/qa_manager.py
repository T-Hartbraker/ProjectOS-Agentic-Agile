"""QA Manager aggregation — management evidence, not a fifth assessor verdict."""

from __future__ import annotations

import sqlite3
from typing import Any

from projectos.domain_events import ACTOR_QA, lookup_event_context_for_job
from projectos.errors import OrchestrationError
from projectos.qa_evidence_policy import update_qa_evidence_result
from projectos.qa_gate import emit_qa_gate_evaluation
from projectos.qa_handoff import REQUIRED_ASSURANCE
from projectos.store import OrchestrationJob, append_run_event

QA_MANAGER_ROLE = "QA_MANAGER"


def _aggregate_gate_result(rows: list[sqlite3.Row]) -> str:
    failed = any(str(r["result"]) in {"fail", "stale_rejected"} for r in rows)
    inconclusive = any(str(r["result"]) == "inconclusive" for r in rows)
    pending = any(str(r["result"]) == "pending" for r in rows)
    passed = all(str(r["result"]) == "pass" for r in rows) if rows else False
    if failed:
        return "fail"
    if inconclusive:
        return "inconclusive"
    if pending or not rows:
        return "pending"
    if passed:
        return "pass"
    return "pending"


def execute_qa_manager_aggregation(
    conn: sqlite3.Connection,
    job: OrchestrationJob,
) -> dict[str, Any]:
    """Aggregate required assessor evidence for the delivery candidate."""
    candidate = job.source_candidate_sha or job.base_git_sha
    if not candidate:
        raise OrchestrationError("QA Manager job missing source candidate")
    if job.source_delivery_job_id is None:
        raise OrchestrationError("QA Manager job missing source delivery provenance")

    rows = conn.execute(
        """
        SELECT e.result, e.assurance_role, e.candidate_git_sha
        FROM qa_evidence e
        WHERE e.delivery_job_id = ?
          AND e.candidate_git_sha = ?
          AND e.assurance_role IN ({})
        ORDER BY e.assurance_role ASC
        """.format(",".join("?" * len(REQUIRED_ASSURANCE))),
        (job.source_delivery_job_id, candidate, *REQUIRED_ASSURANCE),
    ).fetchall()

    missing = set(REQUIRED_ASSURANCE) - {str(r["assurance_role"]) for r in rows}
    if missing:
        raise OrchestrationError(
            f"QA Manager missing assessor evidence for roles: {sorted(missing)}"
        )

    aggregate = _aggregate_gate_result(rows)
    evidence_ref = f"qa-manager-aggregate:{candidate}:{aggregate}"

    mgmt_row = conn.execute(
        """
        SELECT id FROM qa_evidence
        WHERE assurance_job_id = ? AND candidate_git_sha = ?
        """,
        (job.id, candidate),
    ).fetchone()
    if mgmt_row is None:
        raise OrchestrationError(
            f"QA Manager management evidence missing for job {job.human_id}"
        )

    update_qa_evidence_result(
        conn,
        assurance_job_id=job.id,
        candidate_git_sha=candidate,
        new_result=aggregate,
        evidence_ref=evidence_ref,
    )
    append_run_event(
        conn,
        job.id,
        "qa.manager_aggregated",
        status="SUCCEEDED",
        message=f"QA Manager aggregated gate={aggregate} for {candidate}",
        payload={
            "candidate_git_sha": candidate,
            "aggregate_result": aggregate,
            "assessor_results": {str(r["assurance_role"]): str(r["result"]) for r in rows},
        },
    )
    event_ctx = lookup_event_context_for_job(conn, job.id)
    if event_ctx is not None:
        from projectos.domain_events import emit_projectos_event

        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="QA_MANAGER_AGGREGATED",
            summary=f"QA Manager aggregated {aggregate} for {candidate}",
            actor_id=ACTOR_QA,
            phase="QA_GATE",
            detail_level="normal",
            evidence={
                "candidate_git_sha": candidate,
                "aggregate_result": aggregate,
                "assessor_results": {str(r["assurance_role"]): str(r["result"]) for r in rows},
            },
        )
        emit_qa_gate_evaluation(
            conn,
            project_id=job.project_human_id,
            event_context=event_ctx,
            candidate_git_sha=candidate,
            run_id=event_ctx.run_id,
        )
    return {
        "aggregate_result": aggregate,
        "candidate_git_sha": candidate,
        "assessor_count": len(rows),
    }
