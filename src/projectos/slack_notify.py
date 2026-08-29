"""Idempotent Slack notices for important delivery events. Not a worker log."""

from __future__ import annotations

import uuid
from typing import Any, Callable

from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.services.context import ServiceContext
from projectos.services.facades import ProjectQueryService, RegistryService
from projectos.store import (
    insert_slack_notification,
    list_slack_bindings_for_project,
    list_slack_notifications_for_project,
)

NOTIFY_KINDS = frozenset(
    {
        "iteration_review_ready",
        "sponsor_decision_required",
        "blocking_qa_failure",
        "release_ready",
        "released",
        "recovery_failure",
    }
)
_RECOVERY_MARKERS = ("recover", "salvage", "reconcile")
_ACTIVE = frozenset({"READY", "LEASED", "RUNNING"})


def dashboard_path(project_human_id: str, suffix: str = "") -> str:
    base = f"/projects/{project_human_id}"
    return base + suffix if suffix else base


def collect_candidates(ctx: ServiceContext, project_human_id: str) -> list[dict[str, str]]:
    queries = ProjectQueryService(ctx)
    summary = queries.summary(project_human_id)
    jobs = queries.jobs(project_human_id)
    quality = queries.quality(project_human_id)
    releases = queries.releases(project_human_id)
    from projectos.decisions import list_decisions

    initialize_database(ctx.db_path)
    with connection(ctx.db_path) as conn:
        decisions = list_decisions(conn, project_human_id, status="OPEN")["decisions"]

    items: list[dict[str, str]] = []
    iteration = summary.current_iteration_human_id
    if iteration:
        in_iter = [job for job in jobs if job.iteration_human_id == iteration]
        active = [job for job in in_iter if job.status in _ACTIVE]
        succeeded = [job for job in in_iter if job.status == "SUCCEEDED"]
        if in_iter and succeeded and not active:
            items.append(
                {
                    "kind": "iteration_review_ready",
                    "entity_human_id": iteration,
                    "text": (
                        f"{project_human_id}: iteration {iteration} is ready for review. "
                        f"{dashboard_path(project_human_id, '/reports')}"
                    ),
                    "dashboard_path": dashboard_path(project_human_id, "/reports"),
                }
            )
    for decision in decisions:
        hid = str(decision.get("decision_human_id") or "")
        if not hid:
            continue
        items.append(
            {
                "kind": "sponsor_decision_required",
                "entity_human_id": hid,
                "text": (
                    f"{project_human_id}: Sponsor decision {hid} required. "
                    f"{dashboard_path(project_human_id, '/decisions')}"
                ),
                "dashboard_path": dashboard_path(project_human_id, "/decisions"),
            }
        )
    for defect in quality.get("defects") or []:
        if defect.get("status") != "open":
            continue
        hid = str(defect.get("defect_human_id") or "")
        if not hid:
            continue
        items.append(
            {
                "kind": "blocking_qa_failure",
                "entity_human_id": hid,
                "text": (
                    f"{project_human_id}: blocking QA failure {hid}. "
                    f"{dashboard_path(project_human_id, '/quality')}"
                ),
                "dashboard_path": dashboard_path(project_human_id, "/quality"),
            }
        )
    for latest in releases.get("releases") or []:
        rel_id = str(latest.get("release_human_id") or "")
        gate = str(latest.get("gate") or "")
        status = str(latest.get("status") or "")
        rel_path = (
            dashboard_path(project_human_id, f"/releases/{rel_id}")
            if rel_id
            else dashboard_path(project_human_id, "/releases")
        )
        if rel_id and gate == "ready" and status != "SUCCEEDED":
            items.append(
                {
                    "kind": "release_ready",
                    "entity_human_id": rel_id,
                    "text": f"{project_human_id}: release {rel_id} is ready. {rel_path}",
                    "dashboard_path": rel_path,
                }
            )
        if rel_id and status == "SUCCEEDED":
            items.append(
                {
                    "kind": "released",
                    "entity_human_id": rel_id,
                    "text": f"{project_human_id}: release {rel_id} released. {rel_path}",
                    "dashboard_path": rel_path,
                }
            )
    for job in jobs:
        if job.status != "FAILED":
            continue
        err = str(job.last_error or "").lower()
        significant = job.queue in {"RELEASE", "INTEGRATION"} or any(
            marker in err for marker in _RECOVERY_MARKERS
        )
        if not significant:
            continue
        items.append(
            {
                "kind": "recovery_failure",
                "entity_human_id": job.human_id,
                "text": (
                    f"{project_human_id}: recovery/release failure {job.human_id}. "
                    f"{dashboard_path(project_human_id, '/jobs')}"
                ),
                "dashboard_path": dashboard_path(project_human_id, "/jobs"),
            }
        )
    return items


def post_due_notifications(
    ctx: ServiceContext,
    project_human_id: str,
    *,
    poster: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    RegistryService(ctx).show(project_human_id)
    initialize_database(ctx.db_path)
    with connection(ctx.db_path) as conn:
        bindings = list_slack_bindings_for_project(conn, project_human_id)
        if not bindings:
            return {
                "project_human_id": project_human_id,
                "posted": [],
                "already_posted": [],
                "notice": "No Slack bindings; nothing posted.",
            }
        channel = bindings[0]
        candidates = collect_candidates(ctx, project_human_id)
        posted: list[dict[str, Any]] = []
        already: list[str] = []
        existing = {
            (row["kind"], row["entity_human_id"])
            for row in list_slack_notifications_for_project(conn, project_human_id)
        }
        for item in candidates:
            key = (item["kind"], item["entity_human_id"])
            if key in existing:
                already.append(item["entity_human_id"])
                continue
            row = insert_slack_notification(
                conn,
                notification_human_id=f"NTF-{uuid.uuid4().hex[:12]}",
                project_human_id=project_human_id,
                kind=item["kind"],
                entity_human_id=item["entity_human_id"],
                channel_id=str(channel["channel_id"]),
                team_id=str(channel.get("team_id") or ""),
                thread_ts=str(channel.get("thread_ts") or ""),
                text=item["text"],
                dashboard_path=item["dashboard_path"],
            )
            if poster is not None:
                poster(row)
            posted.append(row)
            existing.add(key)
    return {
        "project_human_id": project_human_id,
        "posted": posted,
        "already_posted": already,
        "notice": "Notifications are idempotent and omit repository paths.",
    }


def list_notifications(ctx: ServiceContext, project_human_id: str) -> dict[str, Any]:
    RegistryService(ctx).show(project_human_id)
    initialize_database(ctx.db_path)
    with connection(ctx.db_path) as conn:
        rows = list_slack_notifications_for_project(conn, project_human_id)
    return {"project_human_id": project_human_id, "notifications": rows}
