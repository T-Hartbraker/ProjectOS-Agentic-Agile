"""Deterministic Slack project resolution and channel authorization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from projectos.errors import ConflictError, OrchestrationError
from projectos.registry import ProjectRegistry, load_registry_or_empty
from projectos.repository import load_repository_identity
from projectos.slack import resolve_inbound
from projectos.store import (
    get_slack_binding,
    get_slack_project_context,
    is_slack_interface_channel,
    list_slack_interface_channels,
    require_safe_id,
    upsert_slack_project_context,
)

CONTEXT_TTL_DAYS = 30
DM_CHANNEL_PREFIX = "D"


@dataclass(frozen=True)
class SlackRequestContext:
    team_id: str
    channel_id: str
    thread_ts: str | None
    user_id: str


@dataclass(frozen=True)
class ProjectResolveResult:
    ok: bool
    project_human_id: str | None = None
    resolved_via: str | None = None
    binding_human_id: str | None = None
    clarify: bool = False
    clarify_text: str | None = None
    unauthorized: bool = False
    unauthorized_text: str | None = None
    unknown_project: bool = False
    unknown_text: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _team_key(team_id: str | None) -> str:
    return str(team_id or "").strip()


def _is_dm_channel(channel_id: str) -> bool:
    return str(channel_id or "").strip().upper().startswith(DM_CHANNEL_PREFIX)


def authorize_slack_channel(
    conn,
    *,
    channel_id: str,
    team_id: str | None = None,
) -> bool:
    channel = require_safe_id(channel_id, label="channel_id")
    team = _team_key(team_id)
    if _is_dm_channel(channel):
        return True
    if is_slack_interface_channel(conn, channel_id=channel, team_id=team):
        return True
    if get_slack_binding(conn, team_id=team, channel_id=channel, thread_ts="") is not None:
        return True
    return False


def unauthorized_channel_text() -> str:
    return (
        "This Slack channel is not authorized for ProjectOS. "
        "Add it as a global interface channel in ProjectOS Settings → Integrations → Slack, "
        "or bind it to a project for legacy routing."
    )


def _enabled_projects(registry: ProjectRegistry) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in registry.enabled_projects():
        name = None
        try:
            identity = load_repository_identity(entry.repository_root)
            name = identity.project_name
        except Exception:
            name = None
        items.append(
            {
                "project_human_id": entry.project_human_id,
                "project_name": name,
                "enabled": entry.enabled,
            }
        )
    return items


def format_project_clarification(registry: ProjectRegistry) -> str:
    projects = _enabled_projects(registry)
    lines = ["Which ProjectOS project should I use?", ""]
    if not projects:
        lines.append("No active projects are registered.")
        return "\n".join(lines)
    for item in projects:
        label = item["project_human_id"]
        if item.get("project_name"):
            label = f"{item['project_human_id']} — {item['project_name']}"
        lines.append(label)
    lines.extend(
        [
            "",
            "Use `/projectos use PRJ-003` to set your project context,",
            "or `/projectos PRJ-003 status` for a one-off command.",
        ]
    )
    return "\n".join(lines)


def lookup_project_identifier(
    registry: ProjectRegistry,
    token: str,
) -> tuple[str | None, str | None]:
    """Resolve a human id or unique project name. Returns (id, error)."""
    raw = str(token or "").strip()
    if not raw:
        return None, "project identifier is required"
    try:
        require_safe_id(raw, label="project_human_id")
        entry = registry.get(raw)
        if entry is None:
            lowered = raw.lower()
            for candidate in registry.projects:
                if candidate.project_human_id.lower() == lowered:
                    entry = candidate
                    break
        if entry is None:
            return None, f"Unknown project {raw!r}."
        if not entry.enabled:
            return None, f"Project {raw!r} is disabled."
        return entry.project_human_id, None
    except OrchestrationError:
        pass
    lowered = raw.lower()
    matches: list[str] = []
    for entry in registry.enabled_projects():
        try:
            identity = load_repository_identity(entry.repository_root)
            name = str(identity.project_name or "").strip()
        except Exception:
            name = ""
        if name and name.lower() == lowered:
            matches.append(entry.project_human_id)
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"Project name {raw!r} is ambiguous. Use a project id such as PRJ-003."
    return None, f"Unknown project {raw!r}."


def set_session_project(
    conn,
    *,
    team_id: str | None,
    channel_id: str,
    thread_ts: str | None,
    user_id: str,
    project_human_id: str,
) -> dict[str, Any]:
    team = _team_key(team_id)
    channel = require_safe_id(channel_id, label="channel_id")
    user = require_safe_id(user_id, label="user_id")
    thread = str(thread_ts or "").strip()
    project = require_safe_id(project_human_id, label="project_human_id")
    expires = (_now() + timedelta(days=CONTEXT_TTL_DAYS)).replace(microsecond=0)
    expires_at = expires.isoformat().replace("+00:00", "Z")
    return upsert_slack_project_context(
        conn,
        team_id=team,
        channel_id=channel,
        thread_ts=thread,
        user_id=user,
        project_human_id=project,
        expires_at=expires_at,
    )


def format_use_confirmation(project_human_id: str, *, project_name: str | None = None) -> str:
    label = project_human_id
    if project_name:
        label = f"{project_human_id} ({project_name})"
    return f"Project context set to {label}. Try `/projectos status`."


def format_projects_list(registry: ProjectRegistry) -> str:
    projects = _enabled_projects(registry)
    if not projects:
        return "No active ProjectOS projects are registered."
    lines = ["ProjectOS projects:", ""]
    for item in projects:
        label = item["project_human_id"]
        if item.get("project_name"):
            label = f"{item['project_human_id']} — {item['project_name']}"
        lines.append(label)
    return "\n".join(lines)


def resolve_slack_project(
    conn,
    *,
    registry_path: Path | str,
    channel_id: str,
    team_id: str | None = None,
    thread_ts: str | None = None,
    user_id: str | None = None,
    explicit_project: str | None = None,
    require_channel_auth: bool = True,
) -> ProjectResolveResult:
    channel = require_safe_id(channel_id, label="channel_id")
    team = _team_key(team_id)
    thread = str(thread_ts or "").strip()
    user = str(user_id or "").strip()
    registry = load_registry_or_empty(registry_path)

    if require_channel_auth and not authorize_slack_channel(conn, channel_id=channel, team_id=team):
        return ProjectResolveResult(
            ok=False,
            unauthorized=True,
            unauthorized_text=unauthorized_channel_text(),
        )

    if explicit_project:
        project_id, error = lookup_project_identifier(registry, explicit_project)
        if error:
            return ProjectResolveResult(
                ok=False,
                unknown_project=True,
                unknown_text=error,
            )
        assert project_id is not None
        return ProjectResolveResult(
            ok=True,
            project_human_id=project_id,
            resolved_via="explicit_command",
        )

    if user:
        ctx_row = get_slack_project_context(
            conn,
            team_id=team,
            channel_id=channel,
            thread_ts=thread,
            user_id=user,
        )
        if ctx_row is None and thread:
            ctx_row = get_slack_project_context(
                conn,
                team_id=team,
                channel_id=channel,
                thread_ts="",
                user_id=user,
            )
        if ctx_row is not None:
            expires = str(ctx_row.get("expires_at") or "").strip()
            if expires:
                try:
                    exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                    if exp < _now():
                        ctx_row = None
                except ValueError:
                    pass
            if ctx_row is not None:
                return ProjectResolveResult(
                    ok=True,
                    project_human_id=str(ctx_row["project_human_id"]),
                    resolved_via="session",
                )

    try:
        legacy = resolve_inbound(
            conn,
            channel_id=channel,
            team_id=team or None,
            thread_ts=thread or None,
            message_ts=None,
            project_human_id=None,
        )
        return ProjectResolveResult(
            ok=True,
            project_human_id=str(legacy["project_human_id"]),
            resolved_via=str(legacy.get("resolved_via") or "channel"),
            binding_human_id=legacy.get("binding_human_id"),
        )
    except ConflictError as exc:
        return ProjectResolveResult(
            ok=False,
            clarify=True,
            clarify_text=str(exc),
        )
    except OrchestrationError:
        pass

    enabled = registry.enabled_projects()
    if len(enabled) == 1:
        return ProjectResolveResult(
            ok=True,
            project_human_id=enabled[0].project_human_id,
            resolved_via="single_active_project",
        )

    return ProjectResolveResult(
        ok=False,
        clarify=True,
        clarify_text=format_project_clarification(registry),
    )


def list_interface_channels_public(conn) -> list[dict[str, Any]]:
    rows = list_slack_interface_channels(conn)
    return [
        {
            "channel_id": row["channel_id"],
            "team_id": row.get("team_id") or None,
            "is_default": bool(row.get("is_default")),
        }
        for row in rows
    ]
