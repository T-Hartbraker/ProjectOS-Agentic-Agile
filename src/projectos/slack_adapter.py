"""Local Slack adapter. Socket Mode is the default inbound transport."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from projectos.operator import load_operator_config
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH, STATE_DIR
from projectos.services.context import ServiceContext
from projectos.slack_runtime import prepare_slack_socket_startup
from projectos.slack_socket import run_socket_mode
from projectos.slack_state import read_slack_state, write_slack_state

_SHUTDOWN_FLAG = STATE_DIR / "run" / "slack_adapter.shutdown"
_RC_NOT_CONFIGURED = 2
_RC_SHUTDOWN = 3


def request_slack_adapter_shutdown() -> None:
    """Signal the adapter reconnect loop to exit (used by operator stop)."""
    _SHUTDOWN_FLAG.parent.mkdir(parents=True, exist_ok=True)
    _SHUTDOWN_FLAG.write_text("1", encoding="utf-8")


def clear_slack_adapter_shutdown() -> None:
    if _SHUTDOWN_FLAG.exists():
        try:
            _SHUTDOWN_FLAG.unlink()
        except OSError:
            pass


def _shutdown_requested() -> bool:
    return _SHUTDOWN_FLAG.is_file()


def run_slack_adapter(
    ctx: ServiceContext,
    *,
    poll_seconds: float = 30.0,
    max_loops: int | None = None,
    require_enabled: bool = True,
    reconnect: bool = True,
    max_reconnect_attempts: int | None = None,
) -> int:
    cfg = load_operator_config()
    if require_enabled and not cfg.slack_enabled:
        print("Slack adapter is disabled in config/operator.json", file=sys.stderr)
        return 1
    if not cfg.slack_enabled:
        return 0
    _ = poll_seconds
    clear_slack_adapter_shutdown()
    creds = prepare_slack_socket_startup(enabled=True)
    if not creds.get("tokens_ready"):
        write_slack_state(
            {
                "status": "not_configured",
                "detail": "Slack tokens are not configured.",
            }
        )
        return _RC_NOT_CONFIGURED

    if not reconnect or max_loops is not None:
        return run_socket_mode(ctx, max_envelopes=max_loops, require_tokens=True)

    backoff = 1.0
    attempt = 0
    while not _shutdown_requested():
        attempt += 1
        write_slack_state(
            {
                "status": "connecting" if attempt == 1 else "reconnecting",
                "reconnect_attempt": attempt,
                "detail": "Opening Socket Mode connection"
                if attempt == 1
                else f"Reconnect attempt {attempt}",
            }
        )
        before_connected = read_slack_state().get("last_connected_at")
        rc = run_socket_mode(ctx, require_tokens=True)
        if _shutdown_requested():
            write_slack_state(
                {
                    "status": "disconnected",
                    "detail": "Slack adapter stopped by operator.",
                }
            )
            clear_slack_adapter_shutdown()
            return _RC_SHUTDOWN
        if rc == _RC_NOT_CONFIGURED:
            return rc
        if max_reconnect_attempts is not None and attempt >= max_reconnect_attempts:
            return rc
        state = read_slack_state()
        reason = str(state.get("last_disconnect_reason") or "Socket Mode disconnected")
        after_connected = state.get("last_connected_at")
        if after_connected and after_connected != before_connected:
            backoff = 1.0
        write_slack_state(
            {
                "status": "reconnecting",
                "reconnect_attempt": attempt,
                "detail": f"{reason}; retrying in {backoff:.0f}s",
            }
        )
        slept = 0.0
        while slept < backoff and not _shutdown_requested():
            time.sleep(min(0.5, backoff - slept))
            slept += 0.5
        if state.get("status") == "connected":
            backoff = 1.0
        else:
            backoff = min(backoff * 2.0, 60.0)
    write_slack_state(
        {
            "status": "disconnected",
            "detail": "Slack adapter stopped by operator.",
        }
    )
    clear_slack_adapter_shutdown()
    return _RC_SHUTDOWN


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m projectos.slack_adapter")
    parser.add_argument("--config", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--max-loops", type=int, default=None)
    args = parser.parse_args(argv)
    ctx = ServiceContext(db_path=args.db, registry_path=args.config)
    return run_slack_adapter(
        ctx,
        poll_seconds=args.poll_seconds,
        max_loops=args.max_loops,
    )


if __name__ == "__main__":
    raise SystemExit(main())
