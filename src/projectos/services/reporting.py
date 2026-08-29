"""Authoritative report collection. Rendering lives in report_render."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from projectos.constants import ASSURANCE_QUEUES
from projectos.errors import OrchestrationError
from projectos.plan import load_latest_accepted_plan
from projectos.qa_handoff import REQUIRED_ASSURANCE
from projectos.services.quality import build_quality_snapshot
from projectos.services.releases import build_release_list
from projectos.store import (
    OrchestrationJob,
    get_orchestration_control,
    list_agent_memories_for_project,
    list_agent_run_usage_for_project,
    list_assurance_for_project,
    list_integrations_for_project,
    list_invalidations_for_project,
    list_jobs_for_project,
    list_memory_events_for_project,
    list_memory_injections_for_project,
    list_release_artifacts,
    list_run_events_for_project,
    summarize_usage_for_project,
    utc_now_iso,
)

REPORT_SCHEMA_VERSION = 1
REPORT_KINDS: dict[str, str] = {
    "project-status": "Project Status",
    "iteration-review": "Iteration Review",
    "quality": "QA / Quality Summary",
    "release": "Release Report",
    "risks": "Risk / Issue Summary",
    "usage": "Token / Model Usage",
    "learning": "Learning Effectiveness",
}

_ACTIVE = frozenset({"QUEUED", "READY", "LEASED", "RUNNING", "RETRY_WAIT"})
_LEARNING_PREFIXES = ("learning.", "memory.", "qa.", "delivery.", "release.")


def build_report_catalog(project_human_id: str) -> dict[str, Any]:
    from projectos.services.report_render import DOWNLOAD_FORMATS

    return {
        "project_human_id": project_human_id,
        "download_formats": list(DOWNLOAD_FORMATS),
        "reports": [
            {"kind": kind, "title": title} for kind, title in REPORT_KINDS.items()
        ],
    }


def collect_report(
    conn,
    *,
    project_human_id: str,
    kind: str,
    enabled: bool,
    iteration_human_id: str | None = None,
) -> dict[str, Any]:
    prepared = _prepare_reports(
        conn,
        project_human_id=project_human_id,
        enabled=enabled,
        iteration_human_id=iteration_human_id,
    )
    return _finish_report(prepared, kind)


def collect_report_dashboard(
    conn,
    *,
    project_human_id: str,
    enabled: bool,
    iteration_human_id: str | None = None,
) -> dict[str, Any]:
    from projectos.services.report_render import DOWNLOAD_FORMATS
    from projectos.store import list_report_snapshots

    prepared = _prepare_reports(
        conn,
        project_human_id=project_human_id,
        enabled=enabled,
        iteration_human_id=iteration_human_id,
    )
    generated = utc_now_iso()
    reports = [_finish_report(prepared, kind, generated_at=generated) for kind in REPORT_KINDS]
    return {
        "origin": "live",
        "project_human_id": project_human_id,
        "generated_at": generated,
        "iteration_human_id": prepared["selected_iteration"],
        "notice": (
            "This board is live collected status. Saved snapshots and downloaded "
            "files are historical documents and are not the system of record."
        ),
        "download_formats": list(DOWNLOAD_FORMATS),
        "reports": reports,
        "snapshots": list_report_snapshots(conn, project_human_id),
    }


def _prepare_reports(
    conn,
    *,
    project_human_id: str,
    enabled: bool,
    iteration_human_id: str | None,
) -> dict[str, Any]:
    jobs = list_jobs_for_project(conn, project_human_id)
    selected_iteration = iteration_human_id or _current_iteration(jobs, conn, project_human_id)
    scoped_jobs = _jobs_for_iteration(jobs, iteration_human_id)
    restrict = iteration_human_id is not None
    job_ids = {job.human_id for job in scoped_jobs}
    evidence = [
        row
        for row in list_assurance_for_project(conn, project_human_id)
        if not restrict
        or row.get("delivery_job_human_id") in job_ids
        or row.get("assurance_job_human_id") in job_ids
    ]
    invalidations = [
        row
        for row in list_invalidations_for_project(conn, project_human_id)
        if not restrict or row.get("delivery_job_human_id") in job_ids
    ]
    integrations = [
        row
        for row in list_integrations_for_project(conn, project_human_id)
        if not restrict or row.get("iteration_human_id") == iteration_human_id
    ]
    events = [
        event
        for event in list_run_events_for_project(conn, project_human_id, limit=200)
        if not restrict or event.job_human_id in job_ids
    ]
    usage_rows = [
        row
        for row in list_agent_run_usage_for_project(conn, project_human_id)
        if not restrict or row["job_human_id"] in job_ids
    ]
    memories = list_agent_memories_for_project(conn, project_human_id)
    memory_events = list_memory_events_for_project(conn, project_human_id, limit=None)
    injections = [
        row
        for row in list_memory_injections_for_project(conn, project_human_id, limit=None)
        if not restrict or row.get("job_human_id") in job_ids
    ]
    if restrict:
        memories = [
            row
            for row in memories
            if row.get("source_job_human_id") in job_ids
            or any(item.get("memory_human_id") == row["memory_human_id"] for item in injections)
        ]
        memory_events = [
            row
            for row in memory_events
            if row.get("memory_human_id") in {item["memory_human_id"] for item in memories}
        ]
    usage_summary = summarize_usage_for_project(conn, project_human_id)
    control = get_orchestration_control(conn, project_human_id)
    plan = load_latest_accepted_plan(conn, project_human_id)
    quality = build_quality_snapshot(
        project_human_id=project_human_id,
        jobs=scoped_jobs,
        evidence=evidence,
        invalidations=invalidations,
        edges=[],
    )
    artifact_counts = {
        job.human_id: len(list_release_artifacts(conn, project_human_id, job.human_id))
        for job in scoped_jobs
        if job.queue == "RELEASE"
    }
    releases = build_release_list(
        project_human_id=project_human_id,
        jobs=scoped_jobs,
        integrations=integrations,
        quality=quality,
        artifacts_by_release=artifact_counts,
    )
    sources = _sources(
        jobs=scoped_jobs,
        evidence=evidence,
        invalidations=invalidations,
        integrations=integrations,
        events=events,
        usage_rows=usage_rows,
        memories=memories,
        memory_events=memory_events,
        injections=injections,
        control=control,
        plan=plan,
        selected_iteration=selected_iteration,
    )
    builders = {
        "project-status": lambda: _project_status(
            enabled=enabled,
            control=control,
            jobs=scoped_jobs,
            plan=plan,
            quality=quality,
            releases=releases,
            selected_iteration=selected_iteration,
        ),
        "iteration-review": lambda: _iteration_review(
            jobs=scoped_jobs,
            plan=plan,
            quality=quality,
            selected_iteration=selected_iteration,
        ),
        "quality": lambda: _quality_body(quality),
        "release": lambda: _release_body(releases, quality),
        "risks": lambda: _risks(scoped_jobs, quality, invalidations, events),
        "usage": lambda: _usage(usage_summary, usage_rows),
        "learning": lambda: _learning(scoped_jobs, evidence, invalidations, events, usage_rows, memories, memory_events, injections),
    }
    latest_release = (releases.get("releases") or [None])[-1]
    return {
        "project_human_id": project_human_id,
        "selected_iteration": selected_iteration,
        "latest_release": latest_release,
        "sources": sources,
        "builders": builders,
    }


def _finish_report(
    prepared: dict[str, Any], kind: str, *, generated_at: str | None = None
) -> dict[str, Any]:
    report_kind = _require_kind(kind)
    latest_release = prepared["latest_release"]
    envelope = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_kind": report_kind,
        "title": REPORT_KINDS[report_kind],
        "project_human_id": prepared["project_human_id"],
        "iteration_human_id": prepared["selected_iteration"],
        "release_human_id": (latest_release or {}).get("release_human_id"),
        "sources": prepared["sources"],
        "body": prepared["builders"][report_kind](),
    }
    revision = _revision(envelope)
    generated = generated_at or utc_now_iso()
    return {**envelope, "generated_at": generated, "revision": revision, "origin": "live"}


def _require_kind(kind: str) -> str:
    text = str(kind or "").strip().lower()
    if text not in REPORT_KINDS:
        raise OrchestrationError(f"report {kind!r} not found")
    return text


def _revision(envelope: dict[str, Any]) -> str:
    encoded = json.dumps(envelope, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _cite(
    entity_type: str, entity_human_id: str | None, timestamp: str | None
) -> dict[str, Any] | None:
    if not entity_human_id:
        return None
    return {
        "entity_type": entity_type,
        "entity_human_id": str(entity_human_id),
        "timestamp": timestamp,
    }


def _sources(
    *,
    jobs: list[OrchestrationJob],
    evidence: list[dict[str, Any]],
    invalidations: list[dict[str, Any]],
    integrations: list[dict[str, Any]],
    events: list[Any],
    usage_rows: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    memory_events: list[dict[str, Any]],
    injections: list[dict[str, Any]],
    control: dict[str, Any],
    plan: dict[str, Any] | None,
    selected_iteration: str | None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(entity_type: str, entity_human_id: str | None, timestamp: str | None) -> None:
        cited = _cite(entity_type, entity_human_id, timestamp)
        if cited is None:
            return
        key = (cited["entity_type"], cited["entity_human_id"])
        if key in seen:
            return
        seen.add(key)
        items.append(cited)

    for job in jobs:
        add("job", job.human_id, job.updated_at or job.created_at)
    for row in memories:
        add("agent_memory", row.get("memory_human_id"), row.get("created_at") or row.get("updated_at"))
    for row in memory_events:
        add(
            "memory_event",
            f"{row.get('memory_human_id')}:{row.get('event_type')}:{row.get('created_at')}",
            row.get("created_at"),
        )
    for row in injections:
        add(
            "memory_injection",
            f"{row.get('memory_human_id')}@{row.get('job_human_id')}",
            row.get("created_at"),
        )
    for row in evidence:
        add(
            "qa_evidence",
            row.get("assurance_job_human_id") or row.get("delivery_job_human_id"),
            row.get("created_at"),
        )
    for row in invalidations:
        add("invalidation", row.get("delivery_job_human_id"), row.get("created_at"))
    for row in integrations:
        add(
            "integration_run",
            row.get("iteration_human_id") or "integration",
            row.get("updated_at") or row.get("created_at"),
        )
    for event in events:
        add("run_event", f"{event.job_human_id}:{event.event_type}", event.created_at)
    for row in usage_rows:
        add("agent_run", row.get("job_human_id"), row.get("created_at"))
    add("orchestration_control", control.get("project_human_id"), control.get("updated_at"))
    if plan:
        add("accepted_plan", plan.get("iteration_human_id") or selected_iteration or "latest", None)
    return items


def _current_iteration(
    jobs: list[OrchestrationJob], conn, project_human_id: str
) -> str | None:
    plan = load_latest_accepted_plan(conn, project_human_id)
    if plan and plan.get("iteration_human_id"):
        return str(plan.get("iteration_human_id"))
    for job in reversed(jobs):
        if job.iteration_human_id:
            return job.iteration_human_id
    return None


def _jobs_for_iteration(
    jobs: list[OrchestrationJob], iteration_human_id: str | None
) -> list[OrchestrationJob]:
    if not iteration_human_id:
        return jobs
    return [job for job in jobs if job.iteration_human_id == iteration_human_id]


def _job_counts(jobs: list[OrchestrationJob]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    return counts


def _health(enabled: bool, paused: bool, jobs: list[OrchestrationJob]) -> tuple[str, list[str]]:
    if not enabled:
        return "disabled", ["project is disabled in the registry"]
    if paused:
        return "paused", ["orchestration is paused"]
    blocked = sum(1 for job in jobs if job.status == "BLOCKED")
    failed = sum(1 for job in jobs if job.status == "FAILED")
    if blocked:
        return "blocked", [f"{blocked} blocked job(s)"]
    if failed:
        return "degraded", [f"{failed} failed job(s)"]
    return "healthy", []


def _project_status(
    *,
    enabled: bool,
    control: dict[str, Any],
    jobs: list[OrchestrationJob],
    plan: dict[str, Any] | None,
    quality: dict[str, Any],
    releases: dict[str, Any],
    selected_iteration: str | None,
) -> dict[str, Any]:
    paused = bool(control.get("paused"))
    status, reasons = _health(enabled, paused, jobs)
    latest_release = (releases.get("releases") or [None])
    release = latest_release[-1] if latest_release else None
    return {
        "health": status,
        "health_reasons": reasons,
        "enabled": enabled,
        "paused": paused,
        "paused_reason": control.get("paused_reason"),
        "has_accepted_plan": plan is not None,
        "iteration_human_id": selected_iteration,
        "job_counts": _job_counts(jobs),
        "open_assurance_jobs": quality.get("summary", {}).get("open_assurance_jobs", 0),
        "latest_release": release,
        "release_blocking_reasons": quality.get("release_blocking_reasons") or [],
    }


def _iteration_review(
    *,
    jobs: list[OrchestrationJob],
    plan: dict[str, Any] | None,
    quality: dict[str, Any],
    selected_iteration: str | None,
) -> dict[str, Any]:
    work_items = []
    seen: set[str] = set()
    for job in jobs:
        item = job.work_item_human_id
        if item and item not in seen:
            seen.add(item)
            work_items.append(
                {
                    "work_item_human_id": item,
                    "work_item_type": job.work_item_type,
                    "job_human_id": job.human_id,
                }
            )
    return {
        "iteration_human_id": selected_iteration,
        "from_accepted_plan": bool(plan and plan.get("iteration_human_id") == selected_iteration),
        "job_counts": _job_counts(jobs),
        "open_jobs": [
            {
                "job_human_id": job.human_id,
                "queue": job.queue,
                "status": job.status,
                "updated_at": job.updated_at,
            }
            for job in jobs
            if job.status in _ACTIVE
        ],
        "work_items": work_items,
        "assurance": quality.get("summary"),
        "jobs": [
            {
                "job_human_id": job.human_id,
                "queue": job.queue,
                "status": job.status,
                "outcome": job.outcome,
                "work_item_human_id": job.work_item_human_id,
                "updated_at": job.updated_at,
            }
            for job in jobs
        ],
    }


def _quality_body(quality: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": quality.get("summary"),
        "findings": quality.get("findings"),
        "defects": quality.get("defects"),
        "defect_counts": quality.get("defect_counts"),
        "release_blocking_reasons": quality.get("release_blocking_reasons") or [],
        "qa_pass_authority": quality.get("qa_pass_authority"),
    }


def _release_body(releases: dict[str, Any], quality: dict[str, Any]) -> dict[str, Any]:
    items = releases.get("releases") or []
    latest = items[-1] if items else None
    return {
        "latest": latest,
        "releases": items,
        "qa_recommendation": latest.get("qa_recommendation") if latest else "pending",
        "known_findings": quality.get("findings"),
        "release_blocking_reasons": quality.get("release_blocking_reasons") or [],
    }


def _risks(
    jobs: list[OrchestrationJob],
    quality: dict[str, Any],
    invalidations: list[dict[str, Any]],
    events: list[Any],
) -> dict[str, Any]:
    issues = []
    for job in jobs:
        if job.status in {"FAILED", "BLOCKED"} or job.last_error:
            issues.append(
                {
                    "kind": "job",
                    "job_human_id": job.human_id,
                    "status": job.status,
                    "message": job.last_error or job.status,
                    "timestamp": job.updated_at,
                }
            )
    for defect in quality.get("defects") or []:
        if defect.get("status") == "open":
            issues.append(
                {
                    "kind": "defect",
                    "job_human_id": defect.get("delivery_job_human_id"),
                    "status": defect.get("status"),
                    "message": defect.get("defect_human_id"),
                    "timestamp": None,
                }
            )
    for row in invalidations:
        issues.append(
            {
                "kind": "invalidation",
                "job_human_id": row.get("delivery_job_human_id"),
                "status": "invalidated",
                "message": row.get("reason"),
                "timestamp": row.get("created_at"),
            }
        )
    return {
        "issue_count": len(issues),
        "issues": issues,
        "release_blocking_reasons": quality.get("release_blocking_reasons") or [],
        "required_assurance_gaps": [
            role
            for role in REQUIRED_ASSURANCE
            if (quality.get("summary") or {}).get("role_results", {}).get(role) not in {"pass", "passed"}
        ],
        "recent_error_events": [
            {
                "job_human_id": event.job_human_id,
                "event_type": event.event_type,
                "status": event.status,
                "message": event.message,
                "created_at": event.created_at,
            }
            for event in events
            if event.status in {"FAILED", "BLOCKED"} or (event.message and "error" in event.event_type.lower())
        ][:20],
    }


def _usage(summary: dict[str, Any], usage_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "reported": bool(summary.get("reported")),
        "input_tokens": summary.get("input_tokens"),
        "output_tokens": summary.get("output_tokens"),
        "runs_with_usage": summary.get("runs_with_usage"),
        "run_count": summary.get("run_count"),
        "note": (
            "Token totals include only agent runs that recorded numeric usage. "
            "Missing usage is Not reported."
        ),
        "runs": usage_rows,
    }


def _happened_after(
    candidate_at: str | None,
    origin_at: str | None,
    *,
    candidate_id: int | None = None,
    origin_id: int | None = None,
) -> bool:
    left = str(candidate_at or "")
    right = str(origin_at or "")
    if not left or not right:
        return False
    if left > right:
        return True
    if left == right and candidate_id is not None and origin_id is not None:
        return candidate_id > origin_id
    return False


def _related_job_ids(
    seed_ids: set[str],
    jobs: list[OrchestrationJob],
    invalidations: list[dict[str, Any]],
) -> set[str]:
    by_human = {job.human_id: job for job in jobs}
    related = {hid for hid in seed_ids if hid}
    changed = True
    while changed:
        changed = False
        work_items = {
            by_human[hid].work_item_human_id
            for hid in related
            if hid in by_human and by_human[hid].work_item_human_id
        }
        numeric_ids = {by_human[hid].id for hid in related if hid in by_human}
        for job in jobs:
            if job.human_id in related:
                continue
            if job.work_item_human_id and job.work_item_human_id in work_items:
                related.add(job.human_id)
                changed = True
                continue
            if job.source_delivery_job_id is not None and job.source_delivery_job_id in numeric_ids:
                related.add(job.human_id)
                changed = True
        for row in invalidations:
            delivery = row.get("delivery_job_human_id")
            rework = row.get("rework_job_human_id")
            if delivery in related and rework and rework not in related:
                related.add(str(rework))
                changed = True
            if rework in related and delivery and delivery not in related:
                related.add(str(delivery))
                changed = True
    return related


def _learning(
    jobs: list[OrchestrationJob],
    evidence: list[dict[str, Any]],
    invalidations: list[dict[str, Any]],
    events: list[Any],
    usage_rows: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    memory_events: list[dict[str, Any]],
    injections: list[dict[str, Any]],
) -> dict[str, Any]:
    succeeded = sum(1 for job in jobs if job.status == "SUCCEEDED")
    failed = sum(1 for job in jobs if job.status == "FAILED")
    pass_count = sum(1 for row in evidence if row.get("result") in {"pass", "passed"})
    fail_count = sum(1 for row in evidence if row.get("result") in {"fail", "failed"})
    learning_events = [
        event
        for event in events
        if str(event.event_type).startswith(_LEARNING_PREFIXES)
    ]
    run_ok = sum(1 for row in usage_rows if row.get("exit_code") == 0)
    run_fail = sum(1 for row in usage_rows if row.get("exit_code") not in (None, 0))
    by_human = {job.human_id: job for job in jobs}
    events_by_memory: dict[str, list[dict[str, Any]]] = {}
    for row in memory_events:
        events_by_memory.setdefault(str(row["memory_human_id"]), []).append(row)
    injections_by_memory: dict[str, list[dict[str, Any]]] = {}
    for row in injections:
        injections_by_memory.setdefault(str(row["memory_human_id"]), []).append(row)

    rows: list[dict[str, Any]] = []
    unused: list[dict[str, Any]] = []
    reinforced: list[dict[str, Any]] = []
    repeated: list[dict[str, Any]] = []
    for memory in memories:
        mem_id = str(memory["memory_human_id"])
        mem_events = events_by_memory.get(mem_id, [])
        mem_injections = injections_by_memory.get(mem_id, [])
        reinforcement_count = sum(1 for item in mem_events if item.get("event_type") == "reinforced")
        injected_jobs = [str(item["job_human_id"]) for item in mem_injections if item.get("job_human_id")]
        seed = set(injected_jobs)
        if memory.get("source_job_human_id"):
            seed.add(str(memory["source_job_human_id"]))
        related = _related_job_ids(seed, jobs, invalidations)
        first_injection_at = None
        origin_job_id = None
        if mem_injections:
            earliest = min(
                mem_injections,
                key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)),
            )
            first_injection_at = earliest.get("created_at")
            injected = by_human.get(str(earliest.get("job_human_id") or ""))
            origin_job_id = injected.id if injected is not None else None
        later_fail_jobs: list[str] = []
        later_success_jobs: list[str] = []
        later_defects: list[str] = []
        later_rework: list[str] = []
        if first_injection_at:
            source_id = str(memory.get("source_job_human_id") or "")
            for job in jobs:
                if job.human_id not in related or job.human_id == source_id:
                    continue
                if not _happened_after(
                    job.created_at,
                    first_injection_at,
                    candidate_id=job.id,
                    origin_id=origin_job_id,
                ):
                    continue
                if job.status == "FAILED":
                    later_fail_jobs.append(job.human_id)
                elif job.status == "SUCCEEDED":
                    later_success_jobs.append(job.human_id)
            for row in evidence:
                delivery = row.get("delivery_job_human_id")
                if delivery not in related or str(delivery) == source_id:
                    continue
                delivery_job = by_human.get(str(delivery)) if delivery else None
                if not _happened_after(
                    row.get("created_at"),
                    first_injection_at,
                    candidate_id=delivery_job.id if delivery_job else None,
                    origin_id=origin_job_id,
                ):
                    continue
                if str(row.get("result") or "") in {"fail", "failed"}:
                    later_defects.append(
                        str(row.get("defect_human_id") or delivery or "qa-fail")
                    )
                    if delivery and delivery not in later_fail_jobs:
                        later_fail_jobs.append(str(delivery))
            for row in invalidations:
                delivery = row.get("delivery_job_human_id")
                rework = row.get("rework_job_human_id")
                if delivery not in related and rework not in related:
                    continue
                delivery_job = by_human.get(str(delivery)) if delivery else None
                later_delivery = str(delivery) in later_fail_jobs or (
                    delivery_job is not None
                    and _happened_after(
                        delivery_job.created_at,
                        first_injection_at,
                        candidate_id=delivery_job.id,
                        origin_id=origin_job_id,
                    )
                )
                if not later_delivery and not _happened_after(
                    row.get("created_at"),
                    first_injection_at,
                    candidate_id=delivery_job.id if delivery_job else None,
                    origin_id=origin_job_id,
                ):
                    continue
                if rework and str(rework) not in later_rework:
                    later_rework.append(str(rework))
                if delivery and str(delivery) not in later_fail_jobs:
                    later_fail_jobs.append(str(delivery))
        recurrence = len(later_fail_jobs) > 1 or (
            bool(later_fail_jobs) and bool(later_defects or later_rework)
        )
        unused_flag = memory.get("status") == "ACTIVE" and not mem_injections
        observation = (
            "never_injected"
            if not mem_injections
            else (
                "later_related_failure_observed"
                if later_fail_jobs or later_defects or later_rework
                else "injected_no_later_related_failure"
            )
        )
        item = {
            "memory_human_id": mem_id,
            "title": memory.get("title"),
            "status": memory.get("status"),
            "agent_role": memory.get("agent_role"),
            "evidence_ref": memory.get("evidence_ref"),
            "source_job_human_id": memory.get("source_job_human_id"),
            "created_at": memory.get("created_at"),
            "reinforcement_count": reinforcement_count,
            "injection_count": len(mem_injections),
            "injected_job_human_ids": injected_jobs,
            "run_lineage": [
                {
                    "job_human_id": item.get("job_human_id"),
                    "agent_run_id": item.get("agent_run_id"),
                    "injected_at": item.get("created_at"),
                }
                for item in mem_injections
            ],
            "subsequent_related_failure_job_human_ids": later_fail_jobs,
            "subsequent_related_success_job_human_ids": later_success_jobs,
            "subsequent_defect_refs": later_defects,
            "subsequent_rework_job_human_ids": later_rework,
            "recurrence_observed": recurrence,
            "unused": unused_flag,
            "observation": observation,
        }
        rows.append(item)
        if unused_flag:
            unused.append(
                {
                    "memory_human_id": mem_id,
                    "title": memory.get("title"),
                    "status": memory.get("status"),
                    "agent_role": memory.get("agent_role"),
                    "source_job_human_id": memory.get("source_job_human_id"),
                }
            )
        if reinforcement_count:
            reinforced.append(
                {
                    "memory_human_id": mem_id,
                    "title": memory.get("title"),
                    "reinforcement_count": reinforcement_count,
                    "status": memory.get("status"),
                }
            )
        if later_fail_jobs or later_defects or later_rework:
            repeated.append(
                {
                    "memory_human_id": mem_id,
                    "title": memory.get("title"),
                    "observation": observation,
                    "subsequent_related_failure_job_human_ids": later_fail_jobs,
                    "subsequent_defect_refs": later_defects,
                    "subsequent_rework_job_human_ids": later_rework,
                    "recurrence_observed": recurrence,
                }
            )

    return {
        "job_success_count": succeeded,
        "job_failure_count": failed,
        "assurance_pass_count": pass_count,
        "assurance_fail_count": fail_count,
        "invalidation_count": len(invalidations),
        "learning_event_count": len(learning_events),
        "agent_run_success_count": run_ok,
        "agent_run_failure_count": run_fail,
        "open_assurance_jobs": sum(
            1 for job in jobs if job.queue in ASSURANCE_QUEUES and job.status in _ACTIVE
        ),
        "memory_count": len(memories),
        "unused_memory_count": len(unused),
        "reinforced_memory_count": len(reinforced),
        "repeated_failure_after_memory_count": len(repeated),
        "caveat": (
            "Joins are observational. A later defect, rework, or success after a "
            "memory was injected is correlation in persisted records, not proof "
            "that the memory caused or prevented the outcome."
        ),
        "note": (
            "Effectiveness rows join memory IDs, source/evidence, run injection "
            "lineage, later related jobs, QA fail evidence, invalidations/rework, "
            "and reinforcement events. Recurrence is recorded only when the same "
            "work item or delivery lineage fails again after injection. These "
            "counts do not claim causal impact."
        ),
        "unused_memories": unused,
        "reinforced_lessons": reinforced,
        "repeated_failure_after_memory": repeated,
        "memories": rows,
        "learning_events": [
            {
                "job_human_id": event.job_human_id,
                "event_type": event.event_type,
                "status": event.status,
                "created_at": event.created_at,
            }
            for event in learning_events[:20]
        ],
    }
