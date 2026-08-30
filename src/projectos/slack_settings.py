"""Global Slack configuration. Tokens live in encrypted local storage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projectos.db import connection
from projectos.errors import OrchestrationError
from projectos.migrate import initialize_database
from projectos.operator import load_operator_config
from projectos.paths import DEFAULT_DB_PATH
from projectos.slack_resolver import list_interface_channels_public
from projectos.slack_state import public_connection
from projectos.slack_status import SETUP_STEPS
from projectos.slack_tokens import token_report
from projectos.store import (
    add_slack_interface_channel,
    list_all_slack_bindings,
    list_slack_interface_channels,
    remove_slack_interface_channel,
    require_safe_id,
    set_default_slack_interface_channel,
)


def _default_channel_id(channels: list[dict[str, Any]]) -> str | None:
    for row in channels:
        if row.get("is_default"):
            return str(row["channel_id"])
    return None


def read_slack_settings(*, db_path: Path | str | None = None) -> dict[str, Any]:
    cfg = load_operator_config()
    tokens = token_report(refresh=True)
    tokens_ready = bool(tokens.get("tokens_ready"))
    from projectos.operator import OperatorPaths, read_pid, pid_is_alive, COMPONENT_SLACK

    paths = OperatorPaths()
    pid = read_pid(paths, COMPONENT_SLACK)
    alive = bool(pid and pid_is_alive(pid))
    connection_info = public_connection(
        enabled=cfg.slack_enabled,
        tokens_ready=tokens_ready,
        adapter_pid=pid,
        adapter_alive=alive if pid else None,
    )
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    interface_channels: list[dict[str, Any]] = []
    bound_channels: list[dict[str, Any]] = []
    try:
        initialize_database(path)
        with connection(path) as conn:
            interface_channels = list_interface_channels_public(conn)
            bindings = list_all_slack_bindings(conn)
            bound_channels = [
                {
                    "project_human_id": row["project_human_id"],
                    "channel_id": row["channel_id"],
                    "team_id": row.get("team_id") or None,
                    "thread_ts": row.get("thread_ts") or None,
                    "channel_name": None,
                }
                for row in bindings
            ]
    except Exception:
        interface_channels = []
        bound_channels = []
    return {
        "enabled": bool(cfg.slack_enabled),
        "mode": "socket",
        "transport": "socket",
        "connection_status": connection_info["status"],
        "detail": connection_info.get("detail") or "",
        "workspace_name": connection_info.get("workspace_name"),
        "team_id": connection_info.get("team_id"),
        "connection_updated_at": connection_info.get("updated_at"),
        "app_token": tokens["app_token"],
        "bot_token": tokens["bot_token"],
        "app_token_present": tokens["app_token_present"],
        "bot_token_present": tokens["bot_token_present"],
        "app_token_valid_prefix": tokens["app_token_valid_prefix"],
        "bot_token_valid_prefix": tokens["bot_token_valid_prefix"],
        "signing_secret_present": tokens["signing_secret_present"],
        "app_token_configured": tokens["app_token_present"],
        "bot_token_configured": tokens["bot_token_present"],
        "app_token_source": tokens.get("app_token_source") or "none",
        "bot_token_source": tokens.get("bot_token_source") or "none",
        "configured": tokens.get("configured", False),
        "tokens_ready": tokens_ready,
        "connection_state": tokens.get("connection_state") or "not_configured",
        "storage": tokens.get("storage") or "none",
        "interface_channels": interface_channels,
        "default_channel_id": _default_channel_id(interface_channels),
        "bound_channels": bound_channels,
        "setup_steps": list(SETUP_STEPS),
    }


def update_slack_settings(
    updates: dict[str, Any],
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    initialize_database(path)
    with connection(path) as conn:
        add_channels = updates.get("add_interface_channels") or []
        remove_channels = updates.get("remove_interface_channels") or []
        default_channel = updates.get("default_channel_id")
        default_team = str(updates.get("default_team_id") or "").strip()
        if not isinstance(add_channels, list) or not isinstance(remove_channels, list):
            raise OrchestrationError("interface channel lists must be arrays")
        for item in add_channels:
            if not isinstance(item, dict):
                raise OrchestrationError("each interface channel must be an object")
            channel_id = str(item.get("channel_id") or "").strip()
            if not channel_id:
                raise OrchestrationError("channel_id is required")
            require_safe_id(channel_id, label="channel_id")
            team_id = str(item.get("team_id") or "").strip()
            is_default = bool(item.get("is_default"))
            add_slack_interface_channel(
                conn,
                channel_id=channel_id,
                team_id=team_id,
                is_default=is_default,
            )
        for item in remove_channels:
            if isinstance(item, dict):
                channel_id = str(item.get("channel_id") or "").strip()
                team_id = str(item.get("team_id") or "").strip()
            else:
                channel_id = str(item or "").strip()
                team_id = ""
            if channel_id:
                remove_slack_interface_channel(conn, channel_id=channel_id, team_id=team_id)
        if default_channel:
            channel_id = str(default_channel).strip()
            require_safe_id(channel_id, label="channel_id")
            row = set_default_slack_interface_channel(
                conn,
                channel_id=channel_id,
                team_id=default_team,
            )
            if row is None:
                add_slack_interface_channel(
                    conn,
                    channel_id=channel_id,
                    team_id=default_team,
                    is_default=True,
                )
    return read_slack_settings(db_path=path)
