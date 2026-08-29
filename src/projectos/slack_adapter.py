"""Local Slack adapter. Socket Mode is the default inbound transport."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from projectos.operator import load_operator_config
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.services.context import ServiceContext
from projectos.slack_socket import run_socket_mode


def run_slack_adapter(
    ctx: ServiceContext,
    *,
    poll_seconds: float = 30.0,
    max_loops: int | None = None,
    require_enabled: bool = True,
) -> int:
    cfg = load_operator_config()
    if require_enabled and not cfg.slack_enabled:
        print("Slack adapter is disabled in config/operator.json", file=sys.stderr)
        return 1
    if not cfg.slack_enabled:
        return 0
    _ = poll_seconds
    return run_socket_mode(ctx, max_envelopes=max_loops, require_tokens=True)


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
