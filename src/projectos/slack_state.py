"""Persisted Slack Socket Mode connection status. No secrets."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from projectos.paths import STATE_DIR

STATE_PATH = STATE_DIR / "run" / "slack_socket.json"
MAX_ENVELOPES = 500

VALID_STATUSES = frozenset(
    {
        "disabled",
        "not_configured",
        "connecting",
        "connected",
        "reconnecting",
        "disconnected",
        "process_dead",
        "error",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _empty() -> dict[str, Any]:
    return {
        "status": "disconnected",
        "detail": "",
        "workspace_name": None,
        "team_id": None,
        "updated_at": None,
        "seen_envelopes": [],
    }


def read_slack_state(path: Path | None = None) -> dict[str, Any]:
    target = path or STATE_PATH
    if not target.is_file():
        return _empty()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    state = _empty()
    status = str(raw.get("status") or "disconnected")
    state["status"] = status if status in VALID_STATUSES else "disconnected"
    state["detail"] = str(raw.get("detail") or "")
    state["workspace_name"] = raw.get("workspace_name") or None
    state["team_id"] = raw.get("team_id") or None
    state["updated_at"] = raw.get("updated_at")
    envelopes = raw.get("seen_envelopes") or []
    if isinstance(envelopes, list):
        state["seen_envelopes"] = [str(item) for item in envelopes][-MAX_ENVELOPES:]
    return state


def write_slack_state(updates: dict[str, Any], *, path: Path | None = None) -> dict[str, Any]:
    target = path or STATE_PATH
    current = read_slack_state(target)
    if "seen_envelopes" in updates:
        current["seen_envelopes"] = list(updates["seen_envelopes"])[-MAX_ENVELOPES:]
    for key in ("status", "detail", "workspace_name", "team_id"):
        if key in updates:
            current[key] = updates[key]
    if current["status"] not in VALID_STATUSES:
        current["status"] = "error"
    current["updated_at"] = _now()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def remember_envelope(envelope_id: str, *, path: Path | None = None) -> bool:
    """Return True if this envelope was already processed."""
    envelope_id = str(envelope_id or "").strip()
    if not envelope_id:
        return False
    state = read_slack_state(path)
    seen = list(state.get("seen_envelopes") or [])
    if envelope_id in seen:
        return True
    seen.append(envelope_id)
    write_slack_state({"seen_envelopes": seen}, path=path)
    return False


def public_connection(
    *,
    enabled: bool,
    tokens_ready: bool,
    path: Path | None = None,
    adapter_pid: int | None = None,
    adapter_alive: bool | None = None,
) -> dict[str, Any]:
    state = read_slack_state(path)
    if not enabled:
        status = "disabled"
        detail = "Slack adapter is disabled in operator config."
    elif not tokens_ready:
        status = "not_configured"
        detail = "Add Slack tokens in Settings → Integrations → Slack, then restart ProjectOS."
    else:
        persisted = str(state.get("status") or "disconnected")
        detail = str(state.get("detail") or "")
        if adapter_alive is False and adapter_pid:
            status = "process_dead"
            if not detail:
                detail = f"Slack adapter process {adapter_pid} is not running."
        elif persisted == "connected" and adapter_alive is False:
            status = "process_dead"
            if not detail:
                detail = "Persisted connection state is stale; adapter process is not running."
        else:
            status = persisted
        if status == "not_configured":
            status = "disconnected"
            if not detail:
                detail = "Tokens are configured. Restart ProjectOS so the Slack adapter can connect."
    return {
        "mode": "socket",
        "status": status,
        "detail": detail,
        "workspace_name": state.get("workspace_name"),
        "team_id": state.get("team_id"),
        "updated_at": state.get("updated_at"),
        "reconnect_attempt": state.get("reconnect_attempt"),
        "last_disconnect_reason": state.get("last_disconnect_reason"),
        "adapter_alive": adapter_alive,
        "adapter_pid": adapter_pid,
    }
