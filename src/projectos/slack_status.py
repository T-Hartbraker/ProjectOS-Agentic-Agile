"""Public Slack connection status. Tokens never appear in this payload."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projectos.paths import DEFAULT_DB_PATH

SETUP_STEPS = [
    "Create the ProjectOS Slack app from integrations/slack/projectos-slack-manifest.yaml.",
    "Generate an App-Level Token (xapp-) with connections:write.",
    "Install the app to the workspace and copy the Bot Token (xoxb-).",
    "In Settings → Integrations → Slack, enter both tokens once. They are saved securely on this PC.",
    "Add your #projectos channel as a global interface channel in the same settings page.",
    "In Slack, run `/projectos use PRJ-###` then `/projectos status`.",
]


def public_slack_status(*, db_path: Path | str | None = None, slack_enabled: bool | None = None) -> dict[str, Any]:
    from projectos.slack_settings import read_slack_settings

    settings = read_slack_settings(db_path=db_path)
    if slack_enabled is not None:
        settings = {**settings, "enabled": bool(slack_enabled)}
    bound = None
    bindings = settings.get("bound_channels") or []
    if bindings:
        row = bindings[0]
        bound = {
            "project_human_id": row["project_human_id"],
            "channel_id": row["channel_id"],
            "team_id": row.get("team_id") or None,
            "thread_ts": row.get("thread_ts") or None,
            "channel_name": None,
        }
    return {
        "mode": settings["mode"],
        "connection_status": settings["connection_status"],
        "app_token": settings["app_token"],
        "bot_token": settings["bot_token"],
        "workspace_name": settings.get("workspace_name"),
        "team_id": settings.get("team_id"),
        "bound_channel": bound,
        "bound_channels": bindings,
        "interface_channels": settings.get("interface_channels") or [],
        "default_channel_id": settings.get("default_channel_id"),
        "setup_steps": settings.get("setup_steps") or list(SETUP_STEPS),
        "detail": settings.get("detail") or "",
    }
