"""Typed QA / assurance facts for Sponsor queries — distinct semantic fields."""

from __future__ import annotations

import sqlite3
from typing import Any

from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.services.facades import ProjectQueryService
from projectos.services.context import ServiceContext
from projectos.store import ACTIVE_WORKTREE_STATUSES


def collect_assurance_facts(ctx: ServiceContext, project_id: str) -> dict[str, Any]:
    """Authoritative assurance metrics with non-substitutable field names."""
    jobs = ProjectQueryService(ctx).jobs(project_id)
    assurance_jobs = [j for j in jobs if str(j.queue).startswith("ASSURANCE")]
    qa_jobs_completed = len([j for j in assurance_jobs if j.status == "SUCCEEDED"])
    qa_jobs_failed = len([j for j in assurance_jobs if j.status in {"FAILED", "BLOCKED"}])
    qa_jobs_pending = len(
        [j for j in assurance_jobs if j.status in ACTIVE_WORKTREE_STATUSES | {"READY", "QUEUED"}]
    )

    evidence_rows_total = None
    evidence_rows_passed = None
    evidence_rows_need_attention = None
    reviews_total = None
    reviews_completed = None
    reviews_need_attention = None
    initialize_database(ctx.db_path)
    with connection(ctx.db_path) as conn:
        rows = conn.execute(
            "SELECT result FROM qa_evidence WHERE project_human_id = ?",
            (project_id,),
        ).fetchall()
        from projectos.qa_gate import collect_qa_gate_facts

        gate_facts = collect_qa_gate_facts(conn, project_id=project_id)
    if rows:
        evidence_rows_total = len(rows)
        evidence_rows_passed = len([r for r in rows if str(r["result"]) == "pass"])
        evidence_rows_need_attention = len(
            [r for r in rows if str(r["result"]) in {"fail", "stale_rejected"}]
        )
    reviews_total = gate_facts.get("reviews_total")
    reviews_completed = gate_facts.get("reviews_completed")
    reviews_need_attention = gate_facts.get("reviews_need_attention")

    if gate_facts.get("gate") == "FAILED" or qa_jobs_failed:
        gate_status = "FAILED"
    elif gate_facts.get("gate") == "PASSED":
        gate_status = "PASSED"
    elif qa_jobs_pending or gate_facts.get("gate") == "PENDING":
        gate_status = "PENDING"
    else:
        gate_status = "PENDING"

    return {
        "qa_jobs_total": len(assurance_jobs) if assurance_jobs else None,
        "qa_jobs_completed": qa_jobs_completed if assurance_jobs else None,
        "qa_jobs_failed": qa_jobs_failed if assurance_jobs else None,
        "qa_jobs_pending": qa_jobs_pending if assurance_jobs else None,
        "assurance_evidence_rows_total": evidence_rows_total,
        "assurance_evidence_rows_passed": evidence_rows_passed,
        "assurance_evidence_rows_need_attention": evidence_rows_need_attention,
        "reviews_total": reviews_total,
        "reviews_completed": reviews_completed,
        "reviews_need_attention": reviews_need_attention,
        "assurance_reviews_completed": reviews_completed,
        "assurance_reviews_need_attention": reviews_need_attention,
        "tests_total": None,
        "tests_passed": None,
        "tests_failed": None,
        "tests_skipped": None,
        "gate_status": gate_status,
        "semantic_rules": {
            "qa_jobs_total": "Count of ASSURANCE queue orchestration jobs in project scope.",
            "reviews_total": "Count of qa_evidence rows used by QA gate evaluation.",
            "assurance_evidence_rows_total": "Count of qa_evidence table rows.",
            "never_substitute": (
                "Do not describe evidence rows or reviews as QA jobs unless qa_jobs_total is set. "
                "Do not describe QA jobs as reviews unless reviews_total is set."
            ),
        },
    }


def job_detail_facts(ctx: ServiceContext, project_id: str, job_human_id: str) -> dict[str, Any]:
    jobs = ProjectQueryService(ctx).jobs(project_id)
    match = next((j for j in jobs if j.human_id.upper() == job_human_id.upper()), None)
    if match is None:
        return {
            "job_human_id": job_human_id,
            "known": False,
            "unknown_reason": "Job not found in current ProjectOS project scope.",
        }
    return {
        "job_human_id": match.human_id,
        "known": True,
        "queue": match.queue,
        "status": match.status,
        "work_item_human_id": match.work_item_human_id,
        "last_error": match.last_error,
        "outcome": match.outcome,
        "blocker_cause_known": bool(match.last_error),
        "interpretation": (
            "If blocker_cause_known is false, ProjectOS does not contain enough evidence "
            "to determine the exact cause of failure/blocking."
        ),
    }
