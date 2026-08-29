"""Slack Socket Mode runtime bootstrap (fresh credential load on each process start)."""

from __future__ import annotations

from typing import Any

from projectos.slack_state import read_slack_state, write_slack_state
from projectos.slack_tokens import reload_slack_tokens, resolve_slack_credentials


def bootstrap_slack_credentials() -> dict[str, Any]:
    """Load Slack credentials from disk/env as a fresh process would."""
    reload_slack_tokens()
    return resolve_slack_credentials(refresh=True)


def reset_slack_runtime_caches() -> None:
    """Simulate process exit: drop in-process credential caches."""
    reload_slack_tokens()


def slack_runtime_configured() -> bool:
    return bool(bootstrap_slack_credentials().get("tokens_ready"))


def prepare_slack_socket_startup(*, enabled: bool = True) -> dict[str, Any]:
    """Validate credentials and mark Socket Mode state before connecting."""
    creds = bootstrap_slack_credentials()
    if not enabled:
        write_slack_state({"status": "disabled", "detail": "Slack adapter is disabled."})
        return creds
    if not creds.get("tokens_ready"):
        write_slack_state(
            {
                "status": "not_configured",
                "detail": "Add Slack tokens in Settings → Integrations → Slack, then restart ProjectOS.",
            }
        )
        return creds
    write_slack_state(
        {
            "status": "connecting",
            "detail": "Opening Slack Socket Mode connection",
            "workspace_name": None,
            "team_id": None,
        }
    )
    return creds


def mark_slack_socket_connected(
    *,
    workspace_name: str | None = None,
    team_id: str | None = None,
    detail: str = "Socket Mode connected",
) -> dict[str, Any]:
    return write_slack_state(
        {
            "status": "connected",
            "detail": detail,
            "workspace_name": workspace_name,
            "team_id": team_id,
        }
    )


def current_slack_connection_status() -> dict[str, Any]:
    creds = bootstrap_slack_credentials()
    state = read_slack_state()
    return {
        "configured": creds.get("configured"),
        "tokens_ready": creds.get("tokens_ready"),
        "storage": creds.get("storage"),
        "connection_status": state.get("status"),
        "detail": state.get("detail"),
        "workspace_name": state.get("workspace_name"),
        "team_id": state.get("team_id"),
    }
