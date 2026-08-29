"""Ensure optional runtime dependencies are available for the active Python."""

from __future__ import annotations

import subprocess
import sys

_HTTP_REQUIREMENTS = (
    "fastapi>=0.111",
    "uvicorn>=0.30",
    "httpx>=0.27",
    "websocket-client>=1.8",
)


def http_deps_missing() -> list[str]:
    missing: list[str] = []
    try:
        import fastapi  # noqa: F401
    except ImportError:
        missing.append("fastapi")
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        missing.append("uvicorn")
    try:
        import httpx  # noqa: F401
    except ImportError:
        missing.append("httpx")
    try:
        from websocket import WebSocketApp  # noqa: F401
    except ImportError:
        missing.append("websocket-client")
    return missing


def ensure_http_deps(*, quiet: bool = True) -> None:
    if not http_deps_missing():
        return
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        *_HTTP_REQUIREMENTS,
    ]
    if quiet:
        cmd.insert(4, "-q")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "pip install failed").strip()
        raise RuntimeError(
            "ProjectOS HTTP dependencies are missing and could not be installed automatically. "
            f"Run: {sys.executable} -m pip install {' '.join(_HTTP_REQUIREMENTS)}. "
            f"pip said: {detail}"
        )
    still_missing = http_deps_missing()
    if still_missing:
        raise RuntimeError(
            "ProjectOS HTTP dependencies are still missing after install: "
            + ", ".join(still_missing)
        )
