"""Slack status and intake commands. Slack is a communication surface, not a control plane."""

from __future__ import annotations

import re
from typing import Any

from projectos.db import connection
from projectos.errors import OrchestrationError, ProjectctlError
from projectos.migrate import initialize_database
from projectos.projectctl_bridge import create_defect, create_projectctl_entity
from projectos.services.context import ServiceContext
from projectos.services.facades import ProjectQueryService, RegistryService
from projectos.services.reporting import REPORT_KINDS
from projectos.slack import NOTICE, resolve_inbound
from projectos.store import (
    get_slack_intake_item,
    insert_slack_intake_item,
    require_safe_id,
)

STATUS_COMMANDS = frozenset(
    {"status", "iteration", "blockers", "qa", "release", "reports", "learning"}
)
INTAKE_COMMANDS = frozenset({"feedback", "defect"})
COMMANDS = STATUS_COMMANDS | INTAKE_COMMANDS
_SOURCES = frozenset({"customer", "qa", "operator", "slack"})
_PATHISH = re.compile(r"(?:[A-Za-z]:)?[\\/][^\s,;]+")
_BLOCKED = frozenset({"FAILED", "BLOCKED"})
COMMAND_NOTICE = (
    "Slack replies are project-scoped summaries. They are not orchestration "
    "commands and cannot grant Sponsor approval. Slack cannot set defect "
    "severity or priority."
)


def redact(value: str | None) -> str:
    text = _PATHISH.sub("[redacted]", str(value or ""))
    return text.replace("..", "[redacted]").strip()


def parse_created_human_id(stdout: str) -> str | None:
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("Created "):
            token = line.split()[1].rstrip(":")
            return token or None
    return None


def _clean_title(value: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise OrchestrationError("title is required")
    if any(marker in text for marker in ("/", "\\", "..")):
        raise OrchestrationError("title must not contain a path")
    return text[:200]


def _clean_description(value: str | None) -> str:
    text = str(value or "").strip()
    if any(marker in text for marker in ("/", "\\")):
        raise OrchestrationError("description must not contain a path")
    return text[:2000]


def _counts_text(counts: dict[str, int]) -> str:
    parts = [f"{key} {value}" for key, value in sorted(counts.items()) if value]
    return ", ".join(parts) if parts else "none"


def _format_status(summary) -> tuple[str, dict[str, Any]]:
    text = (
        f"{summary.project_human_id} · {'enabled' if summary.enabled else 'disabled'}\n"
        f"jobs: {_counts_text(summary.job_counts)}\n"
        f"iteration: {summary.current_iteration_human_id or 'none'}\n"
        f"release: {summary.current_release_job_human_id or 'none'}"
        f" {summary.current_release_status or '-'}"
    )
    details = {
        "job_counts": dict(summary.job_counts),
        "iteration_human_id": summary.current_iteration_human_id,
        "release_job_human_id": summary.current_release_job_human_id,
        "release_status": summary.current_release_status,
        "has_accepted_plan": summary.has_accepted_plan,
    }
    return text, details


def _format_iteration(summary, jobs) -> tuple[str, dict[str, Any]]:
    iteration = summary.current_iteration_human_id
    scoped = [job for job in jobs if job.iteration_human_id == iteration] if iteration else []
    counts: dict[str, int] = {}
    for job in scoped:
        counts[job.status] = counts.get(job.status, 0) + 1
    text = (
        f"{summary.project_human_id} iteration {iteration or 'none'}\n"
        f"jobs: {_counts_text(counts)}"
    )
    return text, {"iteration_human_id": iteration, "job_counts": counts}


def _format_blockers(project_human_id: str, jobs, quality: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    blocked = [job for job in jobs if job.status in _BLOCKED]
    reasons = [redact(item) for item in (quality.get("release_blocking_reasons") or [])][:5]
    lines = [f"{project_human_id} blockers: {len(blocked)} jobs"]
    for job in blocked[:5]:
        err = redact(job.last_error)[:120] if job.last_error else job.status
        lines.append(f"- {job.human_id} {job.status}: {err}")
    if reasons:
        lines.append("release: " + "; ".join(reasons))
    if len(blocked) <= 5 and not reasons:
        lines.append("no release blockers reported")
    details = {
        "blocked_job_human_ids": [job.human_id for job in blocked],
        "release_blocking_reasons": reasons,
    }
    return "\n".join(lines), details


def _format_qa(quality: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    summary = quality.get("summary") or {}
    defects = quality.get("defects") or []
    open_defects = [item for item in defects if item.get("status") == "open"]
    failed = int(summary.get("failed_count") or 0)
    lines = [
        f"{quality.get('project_human_id')} QA · failed {failed} · "
        f"open defects {len(open_defects)}"
    ]
    for item in open_defects[:5]:
        lines.append(f"- {item.get('defect_human_id')} {item.get('status')}")
    reasons = [redact(item) for item in (quality.get("release_blocking_reasons") or [])][:3]
    if reasons:
        lines.append("blockers: " + "; ".join(reasons))
    details = {
        "failed_count": failed,
        "open_defect_human_ids": [item.get("defect_human_id") for item in open_defects],
        "release_blocking_reasons": reasons,
    }
    return "\n".join(lines), details


def _format_release(project_human_id: str, releases: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    items = releases.get("releases") or []
    latest = items[-1] if items else None
    if latest is None:
        return f"{project_human_id} release: none", {"release_human_id": None}
    rec = latest.get("qa_recommendation") or "pending"
    text = (
        f"{project_human_id} release {latest.get('release_human_id')} "
        f"{latest.get('status')} gate={latest.get('gate')} qa={rec}"
    )
    return text, {
        "release_human_id": latest.get("release_human_id"),
        "status": latest.get("status"),
        "gate": latest.get("gate"),
        "qa_recommendation": rec,
    }


def _format_reports(project_human_id: str) -> tuple[str, dict[str, Any]]:
    links = [
        f"/v1/projects/{project_human_id}/reports/{kind}" for kind in REPORT_KINDS
    ]
    text = f"{project_human_id} reports:\n" + "\n".join(f"- {link}" for link in links)
    return text, {"report_refs": links}


def _format_learning(learning: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    active = learning.get("active_memories") or []
    unused = learning.get("unused_memories") or []
    retired = learning.get("retired_memories") or []
    text = (
        f"{learning.get('project_human_id')} learning · "
        f"active {len(active)} · unused {len(unused)} · retired {len(retired)}. "
        "Correlation only; Slack cannot change memory."
    )
    details = {
        "active_count": len(active),
        "unused_count": len(unused),
        "retired_count": len(retired),
        "active_memory_human_ids": [item.get("memory_human_id") for item in active[:5]],
    }
    return text, details


def _evidence_description(
    description: str,
    *,
    source: str,
    channel_id: str,
    thread_ts: str,
    message_ts: str,
) -> str:
    parts = [description] if description else []
    parts.append("---")
    parts.append(f"Source: slack ({source})")
    parts.append(f"channel={channel_id}")
    parts.append(f"thread={thread_ts or '-'}")
    parts.append(f"message={message_ts or '-'}")
    parts.append("Severity and priority are not set from Slack.")
    return "\n".join(parts)


def run_command(
    ctx: ServiceContext,
    *,
    command: str,
    channel_id: str,
    team_id: str | None = None,
    thread_ts: str | None = None,
    message_ts: str | None = None,
    project_human_id: str | None = None,
    title: str | None = None,
    description: str | None = None,
    source: str | None = None,
    create_defect_fn=None,
    create_feedback_fn=None,
) -> dict[str, Any]:
    action = str(command or "").strip().lower()
    if action not in COMMANDS:
        raise OrchestrationError(f"unknown slack command {command!r}")
    initialize_database(ctx.db_path)
    with connection(ctx.db_path) as conn:
        resolved = resolve_inbound(
            conn,
            channel_id=channel_id,
            team_id=team_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            project_human_id=project_human_id,
        )
        if action in INTAKE_COMMANDS:
            intake = _submit_item(
                conn,
                ctx,
                kind="defect" if action == "defect" else "feedback",
                project_human_id=resolved["project_human_id"],
                title=title,
                description=description,
                source=source,
                team_id=team_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=message_ts,
                create_defect_fn=create_defect_fn,
                create_feedback_fn=create_feedback_fn,
            )
            resolved.update(intake)
            resolved["text"] = intake["text"]
            resolved["details"] = {
                "item_kind": intake["item_kind"],
                "item_human_id": intake["item_human_id"],
                "idempotent": intake["idempotent"],
            }
            return _public(resolved)

    queries = ProjectQueryService(ctx)
    project = resolved["project_human_id"]
    if action == "status":
        text, details = _format_status(queries.summary(project))
    elif action == "iteration":
        text, details = _format_iteration(queries.summary(project), queries.jobs(project))
    elif action == "blockers":
        text, details = _format_blockers(project, queries.jobs(project), queries.quality(project))
    elif action == "qa":
        text, details = _format_qa(queries.quality(project))
    elif action == "release":
        text, details = _format_release(project, queries.releases(project))
    elif action == "reports":
        text, details = _format_reports(project)
    else:
        text, details = _format_learning(queries.learning(project))
    resolved["command"] = action
    resolved["text"] = text
    resolved["details"] = details
    resolved["item_kind"] = None
    resolved["item_human_id"] = None
    resolved["idempotent"] = False
    return _public(resolved)


def _submit_item(
    conn,
    ctx: ServiceContext,
    *,
    kind: str,
    project_human_id: str,
    title: str | None,
    description: str | None,
    source: str | None,
    team_id: str | None,
    channel_id: str,
    thread_ts: str | None,
    message_ts: str | None,
    create_defect_fn,
    create_feedback_fn,
) -> dict[str, Any]:
    heading = _clean_title(title or "")
    body = _clean_description(description)
    origin = str(source or "slack").strip().lower()
    if origin not in _SOURCES:
        raise OrchestrationError("source must be customer, qa, operator, or slack")
    team = str(team_id or "").strip()
    channel = require_safe_id(channel_id, label="channel_id")
    thread = str(thread_ts or "").strip()
    message = str(message_ts or "").strip()
    if message:
        existing = get_slack_intake_item(
            conn,
            team_id=team,
            channel_id=channel,
            message_ts=message,
            item_kind=kind,
        )
        if existing is not None:
            hid = existing["item_human_id"]
            return {
                "command": kind,
                "item_kind": kind,
                "item_human_id": hid,
                "idempotent": True,
                "text": f"Already recorded {kind} {hid}. Severity/priority are not set from Slack.",
            }
    entry = RegistryService(ctx).show(project_human_id)
    evidence = _evidence_description(
        body,
        source=origin,
        channel_id=channel,
        thread_ts=thread,
        message_ts=message,
    )
    try:
        if kind == "defect":
            fn = create_defect_fn or create_defect
            result = fn(entry.repository_root, title=heading, description=evidence)
        else:
            fn = create_feedback_fn or create_projectctl_entity
            result = fn(entry.repository_root, "story", title=heading, description=evidence)
    except ProjectctlError as exc:
        raise OrchestrationError(f"failed to record {kind} via projectctl") from exc
    stdout = getattr(result, "stdout", "") or ""
    hid = parse_created_human_id(stdout)
    if not hid:
        raise OrchestrationError(f"projectctl did not return a {kind} id")
    require_safe_id(hid, label="item_human_id")
    if message:
        insert_slack_intake_item(
            conn,
            project_human_id=project_human_id,
            item_kind=kind,
            item_human_id=hid,
            team_id=team,
            channel_id=channel,
            thread_ts=thread,
            message_ts=message,
        )
    return {
        "command": kind,
        "item_kind": kind,
        "item_human_id": hid,
        "idempotent": False,
        "text": f"Recorded {kind} {hid}. Severity/priority are not set from Slack.",
    }


def _public(payload: dict[str, Any]) -> dict[str, Any]:
    payload.pop("repository_root", None)
    payload.pop("repository_source", None)
    payload.pop("enabled", None)
    payload["notice"] = COMMAND_NOTICE
    return payload
