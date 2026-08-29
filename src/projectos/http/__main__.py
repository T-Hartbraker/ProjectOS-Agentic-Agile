"""python -m projectos.http — local loopback server."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m projectos.http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    from projectos.http.bind_policy import ensure_safe_bind

    ensure_safe_bind(host=args.host, auth_required=False)
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "uvicorn is required to serve the API. Install with: pip install 'agentic-projectos[http]'"
        ) from exc
    uvicorn.run(
        "projectos.http.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
