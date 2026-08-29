"""Slack project binding. Slack is integration metadata, not project state."""

from __future__ import annotations

import uuid
from typing import Any

from projectos.errors import ConflictError, OrchestrationError
from projectos.store import (
    delete_slack_binding,
    get_slack_binding,
    insert_slack_binding,
    insert_slack_message_ref,
    list_slack_bindings_for_project,
    list_slack_message_refs_for_project,
    require_safe_id,
)

NOTICE = (
    "Slack channel, thread, and message identifiers are integration metadata. "
    "Project identity and repository context come from the registry. "
    "Unbound or ambiguous Slack requests are rejected."
)


def _empty(value: str | None) -> str:
    return str(value or "").strip()


def _optional_id(value: str | None, *, label: str) -> str:
    text = _empty(value)
    if not text:
        return ""
    return require_safe_id(text, label=label)


def _binding_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "binding_human_id": row["binding_human_id"],
        "project_human_id": row["project_human_id"],
        "team_id": row["team_id"] or None,
        "channel_id": row["channel_id"],
        "thread_ts": row["thread_ts"] or None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_bindings(conn, project_human_id: str) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    rows = list_slack_bindings_for_project(conn, project)
    refs = list_slack_message_refs_for_project(conn, project)
    return {
        "project_human_id": project,
        "notice": NOTICE,
        "bindings": [_binding_dict(row) for row in rows],
        "message_refs": refs,
    }


def bind_channel(
    conn,
    *,
    project_human_id: str,
    channel_id: str,
    team_id: str | None = None,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    channel = require_safe_id(channel_id, label="channel_id")
    team = _optional_id(team_id, label="team_id")
    thread = _optional_id(thread_ts, label="thread_ts")
    existing = get_slack_binding(conn, team_id=team, channel_id=channel, thread_ts=thread)
    if existing is not None:
        if existing["project_human_id"] != project:
            raise ConflictError(
                "slack location is already bound to "
                f"{existing['project_human_id']!r}"
            )
        return _binding_dict(existing)
    hid = f"SLK-{uuid.uuid4().hex[:12]}"
    row = insert_slack_binding(
        conn,
        binding_human_id=hid,
        project_human_id=project,
        team_id=team,
        channel_id=channel,
        thread_ts=thread,
    )
    return _binding_dict(row)


def unbind_channel(
    conn,
    *,
    project_human_id: str,
    channel_id: str,
    team_id: str | None = None,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    channel = require_safe_id(channel_id, label="channel_id")
    team = _optional_id(team_id, label="team_id")
    thread = _optional_id(thread_ts, label="thread_ts")
    existing = get_slack_binding(conn, team_id=team, channel_id=channel, thread_ts=thread)
    if existing is None:
        raise OrchestrationError("slack binding not found")
    if existing["project_human_id"] != project:
        raise ConflictError(
            "slack binding belongs to "
            f"{existing['project_human_id']!r}, not {project!r}"
        )
    delete_slack_binding(
        conn, team_id=team, channel_id=channel, thread_ts=thread
    )
    return {"ok": True, "project_human_id": project, "binding_human_id": existing["binding_human_id"]}


def resolve_inbound(
    conn,
    *,
    channel_id: str,
    team_id: str | None = None,
    thread_ts: str | None = None,
    message_ts: str | None = None,
    project_human_id: str | None = None,
) -> dict[str, Any]:
    channel = require_safe_id(channel_id, label="channel_id")
    team = _optional_id(team_id, label="team_id")
    thread = _optional_id(thread_ts, label="thread_ts")
    message = _optional_id(message_ts, label="message_ts")
    explicit = _optional_id(project_human_id, label="project_human_id") or None

    thread_row = (
        get_slack_binding(conn, team_id=team, channel_id=channel, thread_ts=thread)
        if thread
        else None
    )
    channel_row = get_slack_binding(conn, team_id=team, channel_id=channel, thread_ts="")

    mapped: dict[str, str] = {}
    if explicit:
        mapped["explicit_command"] = explicit
    if thread_row is not None:
        mapped["thread"] = str(thread_row["project_human_id"])
    if channel_row is not None:
        mapped["channel"] = str(channel_row["project_human_id"])

    projects = set(mapped.values())
    if not projects:
        raise OrchestrationError(
            "slack request is not bound to a project; bind the channel/thread "
            "or pass an explicit registered project_human_id"
        )
    if len(projects) > 1:
        raise ConflictError(
            "slack request is ambiguous: channel, thread, and explicit project "
            "do not resolve to one registered project"
        )
    project = next(iter(projects))
    if "explicit_command" in mapped:
        via = "explicit_command"
        binding_id = (thread_row or channel_row or {}).get("binding_human_id")
    elif thread_row is not None:
        via = "thread"
        binding_id = thread_row["binding_human_id"]
    else:
        via = "channel"
        binding_id = channel_row["binding_human_id"] if channel_row else None

    message_ref = None
    if message:
        insert_slack_message_ref(
            conn,
            project_human_id=project,
            team_id=team,
            channel_id=channel,
            thread_ts=thread,
            message_ts=message,
        )
        message_ref = {
            "project_human_id": project,
            "team_id": team or None,
            "channel_id": channel,
            "thread_ts": thread or None,
            "message_ts": message,
            "created_at": None,
        }

    return {
        "project_human_id": project,
        "binding_human_id": binding_id,
        "resolved_via": via,
        "notice": NOTICE,
        "message_ref": message_ref,
    }
