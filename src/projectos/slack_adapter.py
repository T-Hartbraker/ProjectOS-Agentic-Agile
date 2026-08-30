"""Local Slack adapter. Socket Mode is the default inbound transport."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from projectos.operator import load_operator_config
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.services.context import ServiceContext
from projectos.slack_socket import run_socket_mode
from projectos.slack_state import write_slack_state


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
    if not reconnect or max_loops is not None:
        return run_socket_mode(ctx, max_envelopes=max_loops, require_tokens=True)

    backoff = 1.0
    attempt = 0
    while True:
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
        rc = run_socket_mode(ctx, require_tokens=True)
        if max_reconnect_attempts is not None and attempt >= max_reconnect_attempts:
            return rc
        write_slack_state(
            {
                "status": "reconnecting",
                "reconnect_attempt": attempt,
                "detail": f"Socket Mode disconnected; retrying in {backoff:.0f}s",
            }
        )
        time.sleep(backoff)
        backoff = min(backoff * 2.0, 60.0)

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
