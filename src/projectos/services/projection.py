"""Project-scoped read model for UI and Slack. Polling snapshot; no table dump."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from projectos.constants import ASSURANCE_QUEUES
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.plan import load_latest_accepted_plan
from projectos.qa_handoff import REQUIRED_ASSURANCE
from projectos.services.context import ServiceContext
from projectos.services.facades import ProjectQueryService
from projectos.store import (
    OrchestrationJob,
    get_job,
    get_orchestration_control,
    list_agent_runs_for_project,
    list_assurance_for_project,
    list_eligible_ready_jobs,
    list_expired_lease_job_ids,
    list_integrations_for_project,
    list_invalidations_for_project,
    list_jobs_for_project,
    list_run_events_for_project,
    summarize_usage_for_project,
    utc_now_iso,
)

PROJECTION_SCHEMA_VERSION = 1
POLL_AFTER_SECONDS = 5

_RECOVERABLE_ERROR_MARKERS = (
    "acceptance criteria are empty",
    "lacks resolvable work-item context",
    "Cannot resolve work item",
)

_FAILED_STATUSES = frozenset({"FAILED"})
_BLOCKED_STATUSES = frozenset({"BLOCKED"})
_RETRY_STATUSES = frozenset({"RETRY_WAIT"})
_ACTIVE_STATUSES = frozenset({"QUEUED", "READY", "LEASED", "RUNNING", "RETRY_WAIT"})

_LEARNING_EVENT_PREFIXES = (
    "learning.",
    "memory.",
    "qa.",
    "delivery.",
    "release.",
)


def _is_recoverable_error(text: str | None) -> bool:
    if not text:
        return False
    return any(marker in text for marker in _RECOVERABLE_ERROR_MARKERS)


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


@dataclass
class ProjectProjection:
    schema_version: int
    generated_at: str
    revision: str
    poll_after_seconds: int
    project_human_id: str
    headline: str
    health: dict[str, Any]
    jobs: dict[str, Any]
    assurance: dict[str, Any]
    defects: list[dict[str, Any]]
    integration: dict[str, Any]
    release: dict[str, Any]
    errors: list[dict[str, Any]]
    recoverable: list[dict[str, Any]]
    learning: dict[str, Any]
    approvals: dict[str, Any]
    invalidations: list[dict[str, Any]]
    events: list[dict[str, Any]]

    def as_public_dict(self) -> dict[str, Any]:
        return asdict(self)

    def etag(self) -> str:
        return f'"{self.revision}"'


class ProjectionService:
    """Computed snapshot over Phase 2 / projectctl state. Does not persist a second copy."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self._projects = ProjectQueryService(ctx)

    def snapshot(self, project_human_id: str) -> ProjectProjection:
        entry = self._projects._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            jobs = list_jobs_for_project(conn, project_human_id)
            events = list_run_events_for_project(conn, project_human_id, limit=40)
            control = get_orchestration_control(conn, project_human_id)
            eligible = list_eligible_ready_jobs(
                conn, project_human_id=project_human_id
            )
            expired_ids = list_expired_lease_job_ids(
                conn, project_human_id=project_human_id
            )
            assurance_rows = list_assurance_for_project(conn, project_human_id)
            invalidations = list_invalidations_for_project(conn, project_human_id)
            integrations = list_integrations_for_project(conn, project_human_id)
            agent_runs = list_agent_runs_for_project(conn, project_human_id)
            usage = summarize_usage_for_project(conn, project_human_id)
            plan = load_latest_accepted_plan(conn, project_human_id)
            expired_human_ids: list[str] = []
            for job_id in expired_ids:
                try:
                    expired_human_ids.append(get_job(conn, job_id).human_id)
                except Exception:
                    continue

        paused = bool(control["paused"])
        job_views = [_job_view(job) for job in jobs]
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job.status] = counts.get(job.status, 0) + 1

        errors = _collect_errors(jobs, integrations)
        recoverable = _collect_recoverable(
            jobs,
            paused=paused,
            paused_reason=control["paused_reason"],
            expired_human_ids=expired_human_ids,
        )
        health_status, health_reasons = _health(
            enabled=bool(entry.enabled),
            paused=paused,
            jobs=jobs,
            errors=errors,
        )
        headline = _headline(
            project_human_id,
            health_status,
            health_reasons,
            jobs,
            plan,
        )
        assurance = _assurance_view(jobs, assurance_rows)
        defects = _defect_view(assurance_rows)
        integration = _integration_view(jobs, integrations)
        release = _release_view(jobs)
        approvals = _approvals_view(plan, release, jobs)
        learning = {
            "agent_runs": agent_runs,
            "event_count": sum(
                1
                for event in events
                if str(event.event_type).startswith(_LEARNING_EVENT_PREFIXES)
            ),
            "usage": usage,
        }
        public_events = [
            {
                "job_human_id": event.job_human_id,
                "event_type": event.event_type,
                "status": event.status,
                "message": event.message,
                "created_at": event.created_at,
            }
            for event in events
        ]
        body = {
            "schema_version": PROJECTION_SCHEMA_VERSION,
            "poll_after_seconds": POLL_AFTER_SECONDS,
            "project_human_id": project_human_id,
            "headline": headline,
            "health": {
                "status": health_status,
                "enabled": bool(entry.enabled),
                "paused": paused,
                "paused_reason": control["paused_reason"],
                "reasons": health_reasons,
            },
            "jobs": {
                "counts": counts,
                "eligible_count": len(eligible),
                "items": job_views,
            },
            "assurance": assurance,
            "defects": defects,
            "integration": integration,
            "release": release,
            "errors": errors,
            "recoverable": recoverable,
            "learning": learning,
            "approvals": approvals,
            "invalidations": invalidations,
            "events": public_events,
        }
        revision = _stable_hash(body)
        generated = utc_now_iso()
        return ProjectProjection(
            generated_at=generated,
            revision=revision,
            **body,
        )


def _job_view(job: OrchestrationJob) -> dict[str, Any]:
    return {
        "human_id": job.human_id,
        "queue": job.queue,
        "role": job.agent_role,
        "status": job.status,
        "outcome": job.outcome,
        "iteration_human_id": job.iteration_human_id,
        "work_item_human_id": job.work_item_human_id,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "has_candidate": bool(job.candidate_git_sha),
        "last_error": job.last_error,
    }


def _collect_errors(
    jobs: list[OrchestrationJob], integrations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for job in jobs:
        if job.status in _FAILED_STATUSES | _BLOCKED_STATUSES or job.last_error:
            items.append(
                {
                    "kind": "job",
                    "job_human_id": job.human_id,
                    "status": job.status,
                    "message": job.last_error or job.status,
                }
            )
    for run in integrations:
        if run.get("error") or run.get("status") in {"failed", "conflict", "blocked"}:
            items.append(
                {
                    "kind": "integration",
                    "status": run.get("status"),
                    "message": run.get("error") or run.get("status"),
                }
            )
    return items


def _collect_recoverable(
    jobs: list[OrchestrationJob],
    *,
    paused: bool,
    paused_reason: str | None,
    expired_human_ids: list[str],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if paused:
        items.append(
            {
                "kind": "orchestration_paused",
                "job_human_id": None,
                "message": paused_reason or "orchestration is paused",
            }
        )
    for human_id in expired_human_ids:
        items.append(
            {
                "kind": "expired_lease",
                "job_human_id": human_id,
                "message": "lease expired; recovery can reclaim",
            }
        )
    for job in jobs:
        if job.status in _RETRY_STATUSES:
            items.append(
                {
                    "kind": "retry_wait",
                    "job_human_id": job.human_id,
                    "message": job.last_error or "waiting to retry",
                }
            )
        elif job.status in _BLOCKED_STATUSES and _is_recoverable_error(job.last_error):
            items.append(
                {
                    "kind": "revalidatable_block",
                    "job_human_id": job.human_id,
                    "message": job.last_error,
                }
            )
    return items


def _health(
    *,
    enabled: bool,
    paused: bool,
    jobs: list[OrchestrationJob],
    errors: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not enabled:
        return "disabled", ["project is disabled in the registry"]
    if paused:
        return "paused", ["orchestration is paused"]
    blocked = sum(1 for job in jobs if job.status in _BLOCKED_STATUSES)
    failed = sum(1 for job in jobs if job.status in _FAILED_STATUSES)
    if blocked:
        reasons.append(f"{blocked} blocked job(s)")
        return "blocked", reasons
    if failed:
        reasons.append(f"{failed} failed job(s)")
        return "degraded", reasons
    if errors:
        reasons.append(f"{len(errors)} error condition(s)")
        return "degraded", reasons
    return "healthy", []


def _headline(
    project_human_id: str,
    health: str,
    reasons: list[str],
    jobs: list[OrchestrationJob],
    plan: dict[str, Any] | None,
) -> str:
    iteration = None
    if plan and plan.get("iteration_human_id"):
        iteration = str(plan.get("iteration_human_id"))
    suffix = f" · {iteration}" if iteration else ""
    if health == "healthy":
        ready = sum(1 for job in jobs if job.status == "READY")
        return f"{project_human_id} healthy{suffix} · {ready} ready"
    reason = reasons[0] if reasons else health
    return f"{project_human_id} {health}: {reason}"


def _assurance_view(
    jobs: list[OrchestrationJob], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    required = list(REQUIRED_ASSURANCE)
    pending = [row for row in rows if row["result"] == "pending"]
    passed = [row for row in rows if row["result"] in {"pass", "passed"}]
    failed = [row for row in rows if row["result"] == "fail"]
    stale = [row for row in rows if "stale" in str(row["result"])]
    by_role = {role: "missing" for role in required}
    for row in reversed(rows):
        role = row["assurance_role"]
        if role in by_role:
            by_role[role] = row["result"]
    assurance_jobs = [job for job in jobs if job.queue in ASSURANCE_QUEUES]
    return {
        "required_roles": required,
        "role_results": by_role,
        "pending_count": len(pending),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "stale_count": len(stale),
        "open_assurance_jobs": sum(
            1 for job in assurance_jobs if job.status in _ACTIVE_STATUSES
        ),
        "items": [
            {
                "assurance_role": row["assurance_role"],
                "result": row["result"],
                "delivery_job_human_id": row["delivery_job_human_id"],
                "assurance_job_human_id": row["assurance_job_human_id"],
                "has_candidate": bool(row["candidate_git_sha"]),
                "defect_human_id": row["defect_human_id"],
            }
            for row in rows[:40]
        ],
    }


def _defect_view(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        hid = row.get("defect_human_id")
        if not hid:
            continue
        seen[str(hid)] = {
            "defect_human_id": str(hid),
            "assurance_role": row["assurance_role"],
            "delivery_job_human_id": row["delivery_job_human_id"],
            "result": row["result"],
        }
    return list(seen.values())


def _integration_view(
    jobs: list[OrchestrationJob], runs: list[dict[str, Any]]
) -> dict[str, Any]:
    integ_jobs = [job for job in jobs if job.queue == "INTEGRATION"]
    latest = runs[0] if runs else None
    return {
        "latest": latest,
        "job_counts": _count_by_status(integ_jobs),
        "run_count": len(runs),
    }


def _release_view(jobs: list[OrchestrationJob]) -> dict[str, Any]:
    release_jobs = [job for job in jobs if job.queue == "RELEASE"]
    latest = release_jobs[-1] if release_jobs else None
    outcome = latest.outcome if latest else None
    gate = None
    if outcome in {"GATE_READY", "READY"}:
        gate = "ready"
    elif outcome in {"GATE_REJECTED", "REJECTED"}:
        gate = "rejected"
    elif latest is not None:
        gate = "pending"
    return {
        "latest_job_human_id": latest.human_id if latest else None,
        "status": latest.status if latest else None,
        "outcome": outcome,
        "gate": gate,
        "job_counts": _count_by_status(release_jobs),
    }


def _approvals_view(
    plan: dict[str, Any] | None,
    release: dict[str, Any],
    jobs: list[OrchestrationJob],
) -> dict[str, Any]:
    sponsor = None
    iteration = None
    if plan:
        sponsor = plan.get("sponsor_authority") or plan.get("sponsor_authority")
        iteration = plan.get("iteration_human_id")
    sponsor_ok = str(sponsor or "").lower() in {
        "approved",
        "granted",
        "authorized",
        "sponsor-approved",
    }
    return {
        "has_accepted_plan": plan is not None,
        "sponsor_authority": sponsor,
        "sponsor_granted": bool(plan) and sponsor_ok,
        "iteration_human_id": iteration,
        "release_gate": release.get("gate"),
        "open_pm_jobs": sum(
            1 for job in jobs if job.queue == "PM" and job.status in _ACTIVE_STATUSES
        ),
    }


def _count_by_status(jobs: list[OrchestrationJob]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    return counts
