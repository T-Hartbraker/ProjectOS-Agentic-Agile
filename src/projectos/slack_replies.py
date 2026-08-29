"""Operator-facing Slack replies. Canonical IDs stay in a details section."""

from __future__ import annotations

from typing import Any

from projectos.presentation import queue_label, status_label
from projectos.services.context import ServiceContext
from projectos.services.facades import ProjectQueryService
from projectos.store import ACTIVE_WORKTREE_STATUSES

HELP_TEXT = """ProjectOS commands:
/projectos
/projectos help
/projectos status
/projectos summary
/projectos work
/projectos quality
/projectos releases
/projectos projects
/projectos use PRJ-003
/projectos PRJ-003 status

Use `use` to set your project context for this conversation.
Use `PRJ-### command` for a one-off command without changing context."""

UNBOUND_TEXT = (
    "Which ProjectOS project should I use? "
    "Try `/projectos projects` or `/projectos use PRJ-003`."
)
OVERRIDE_TEXT = (
    "Use `/projectos use PRJ-003` or `/projectos PRJ-003 status` to select a project."
)
UNAUTHORIZED_CHANNEL_TEXT = (
    "This Slack channel is not authorized for ProjectOS. "
    "Add it as a global interface channel in ProjectOS Settings → Integrations → Slack, "
    "or bind it to a project for legacy routing."
)
UNKNOWN_TEXT = "Unknown ProjectOS command. Try /projectos help."


def _details(lines: list[str]) -> str:
    useful = [line for line in lines if line]
    if not useful:
        return ""
    return "\n\nTechnical details:\n" + "\n".join(useful)


def format_help() -> str:
    return HELP_TEXT


def format_summary(ctx: ServiceContext, project_human_id: str) -> str:
    queries = ProjectQueryService(ctx)
    summary = queries.summary(project_human_id)
    jobs = queries.jobs(project_human_id)
    running = [job for job in jobs if job.status in ACTIVE_WORKTREE_STATUSES]
    quality_jobs = [job for job in jobs if str(job.queue).startswith("ASSURANCE")]
    passed = [job for job in quality_jobs if job.status == "SUCCEEDED"]
    failed = [job for job in quality_jobs if job.status in {"FAILED", "BLOCKED"}]
    current = running[0] if running else None
    phase = queue_label(current.queue) if current else "Idle"
    health = "Healthy" if summary.enabled and not failed else "Needs attention"
    if not summary.enabled:
        health = "Paused"
    working = (
        f"{queue_label(current.queue)} is in progress"
        + (f" for {current.work_item_human_id}" if current and current.work_item_human_id else ".")
        if current
        else "Nothing is running."
    )
    if current and current.work_item_human_id:
        working = (
            f"Combining or evaluating {current.work_item_human_id} "
            f"({queue_label(current.queue).lower()})."
        )
    quality = (
        f"All {len(passed)} quality reviews finished."
        if quality_jobs and not failed and len(passed) == len(quality_jobs)
        else f"{len(passed)} quality reviews finished, {len(failed)} need attention."
        if quality_jobs
        else "No quality reviews are in this iteration yet."
    )
    nxt = (
        "Quality review or release verification starts when the current work finishes."
        if current
        else "Submit new work if this iteration is complete."
    )
    body = (
        f"ProjectOS — {project_human_id}\n\n"
        f"Status: {health}\n\n"
        f"Current phase:\n{phase}\n\n"
        f"Currently working:\n{working}\n\n"
        f"Quality:\n{quality}\n\n"
        f"Next:\n{nxt}"
    )
    details = _details(
        [
            f"iteration {summary.current_iteration_human_id}" if summary.current_iteration_human_id else "",
            f"job {current.human_id}" if current else "",
            f"queue {current.queue}" if current else "",
        ]
    )
    return body + details


def format_work(ctx: ServiceContext, project_human_id: str) -> str:
    jobs = ProjectQueryService(ctx).jobs(project_human_id)
    running = [job for job in jobs if job.status in ACTIVE_WORKTREE_STATUSES]
    blocked = [job for job in jobs if job.status in {"FAILED", "BLOCKED"}]
    lines = [f"ProjectOS — {project_human_id}", "", "Active work"]
    if not running:
        lines.append("Nothing is running.")
    for job in running:
        lines.append(f"- {queue_label(job.queue)} is {status_label(job.status).lower()}.")
    lines.extend(["", "Blocked"])
    if not blocked:
        lines.append("Nothing is blocked.")
    for job in blocked[:5]:
        lines.append(f"- {queue_label(job.queue)} is {status_label(job.status).lower()}.")
    details = _details(
        [f"{job.human_id} {job.queue} {job.status}" for job in (running + blocked)[:8]]
    )
    return "\n".join(lines) + details


def format_quality(ctx: ServiceContext, project_human_id: str) -> str:
    jobs = ProjectQueryService(ctx).jobs(project_human_id)
    reviews = [job for job in jobs if str(job.queue).startswith("ASSURANCE")]
    if not reviews:
        return f"ProjectOS — {project_human_id}\n\nQuality:\nNo quality reviews are recorded yet."
    passed = [job for job in reviews if job.status == "SUCCEEDED"]
    pending = [job for job in reviews if job.status in ACTIVE_WORKTREE_STATUSES | {"READY", "QUEUED"}]
    failed = [job for job in reviews if job.status in {"FAILED", "BLOCKED"}]
    if not failed and not pending:
        body = f"ProjectOS — {project_human_id}\n\nQuality:\nAll {len(passed)} independent reviews passed."
    else:
        body = (
            f"ProjectOS — {project_human_id}\n\nQuality:\n"
            f"{len(passed)} passed, {len(failed)} failed, {len(pending)} still in progress."
        )
    details = _details(
        [f"{queue_label(job.queue)}: {status_label(job.status)}" for job in reviews]
    )
    return body + details


def format_releases(
    ctx: ServiceContext,
    project_human_id: str,
    *,
    raw_text: str = "",
) -> str:
    summary = ProjectQueryService(ctx).summary(project_human_id)
    status = status_label(summary.current_release_status) if summary.current_release_status else "Not started"
    body = (
        f"ProjectOS — {project_human_id}\n\nReleases:\n"
        f"Release verification is {status.lower()}."
    )
    details = _details(
        [
            f"release job {summary.current_release_job_human_id}" if summary.current_release_job_human_id else "",
            raw_text.splitlines()[0] if raw_text else "",
        ]
    )
    return body + details


def operator_reply(
    ctx: ServiceContext,
    *,
    command: str,
    project_human_id: str,
    raw_text: str,
) -> str:
    if command in {"help"}:
        return format_help()
    if command in {"", "status", "summary"}:
        return format_summary(ctx, project_human_id)
    if command == "work":
        return format_work(ctx, project_human_id)
    if command in {"quality", "qa"}:
        return format_quality(ctx, project_human_id)
    if command in {"releases", "release"}:
        return format_releases(ctx, project_human_id, raw_text)
    return raw_text
