"""Derive release gate scope from accepted PM plan and run lineage."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from projectos.plan import load_latest_accepted_plan
from projectos.store import OrchestrationJob, list_jobs_for_project, list_jobs_for_run


@dataclass(frozen=True)
class ReleaseScope:
    delivery_story_ids: list[str] = field(default_factory=list)
    required_gate_queues: frozenset[str] = frozenset()
    required_story_shas: dict[str, str] = field(default_factory=dict)
    plan_source: str = "none"


def _plan_jobs(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not plan:
        return []
    jobs = plan.get("jobs") or []
    return [job for job in jobs if isinstance(job, dict)]


def resolve_release_scope(
    conn: sqlite3.Connection,
    job: OrchestrationJob,
) -> ReleaseScope:
    """Scope release validation to the accepted plan and optional run lineage."""
    plan = load_latest_accepted_plan(conn, job.project_human_id)
    plan_jobs = _plan_jobs(plan)

    delivery_ids: list[str] = []
    gate_queues: set[str] = set()
    for plan_job in plan_jobs:
        queue = str(plan_job.get("queue") or "").upper()
        if queue == "DELIVERY":
            story_id = str(plan_job.get("work_item_human_id") or "").strip()
            if story_id:
                delivery_ids.append(story_id)
        elif queue in {"PM", "ARCHITECTURE", "INTEGRATION"}:
            gate_queues.add(queue)

    if not delivery_ids and not gate_queues:
        return ReleaseScope(plan_source="missing_plan")

    jobs = (
        list_jobs_for_run(conn, str(job.run_id))
        if job.run_id
        else list_jobs_for_project(conn, job.project_human_id)
    )
    story_shas: dict[str, str] = {}
    for story_id in delivery_ids:
        delivery = _latest_succeeded_delivery(jobs, story_id)
        if delivery and delivery.candidate_git_sha:
            story_shas[story_id] = delivery.candidate_git_sha

    return ReleaseScope(
        delivery_story_ids=delivery_ids,
        required_gate_queues=frozenset(gate_queues),
        required_story_shas=story_shas,
        plan_source="accepted_plan" if plan else "run_jobs",
    )


def _latest_succeeded_delivery(
    jobs: list[OrchestrationJob], story_id: str
) -> OrchestrationJob | None:
    hits = [
        j
        for j in jobs
        if j.queue == "DELIVERY"
        and j.work_item_human_id == story_id
        and j.status == "SUCCEEDED"
        and j.outcome not in {"INVALIDATED", "SUPERSEDED", "NO_CHANGE"}
        and j.candidate_git_sha
    ]
    return hits[-1] if hits else None


def scope_jobs_for_release(
    conn: sqlite3.Connection,
    job: OrchestrationJob,
) -> list[OrchestrationJob]:
    if job.run_id:
        return list_jobs_for_run(conn, str(job.run_id))
    return list_jobs_for_project(conn, job.project_human_id)
