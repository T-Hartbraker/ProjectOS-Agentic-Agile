"""Local operator packaging: one-start for API, dashboard, daemon, Slack adapter."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from projectos import __version__
from projectos.daemon import get_daemon_status, stop_daemon
from projectos.dashboard_build import ensure_dashboard_built
from projectos.paths import LOGS_DIR, PROJECTOS_ROOT, STATE_DIR, dashboard_is_built
from projectos.runtime_deps import ensure_http_deps
from projectos.services.context import ServiceContext

DEFAULT_OPERATOR_CONFIG_PATH = PROJECTOS_ROOT / "config" / "operator.json"

COMPONENT_API = "api"
COMPONENT_DAEMON = "daemon"
COMPONENT_DASHBOARD = "dashboard"
COMPONENT_SLACK = "slack_adapter"


@dataclass(frozen=True)
class OperatorConfig:
    api_host: str = "127.0.0.1"
    api_port: int = 8787
    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 5173
    daemon_enabled: bool = True
    daemon_poll_seconds: float = 5.0
    slack_enabled: bool = False
    slack_poll_seconds: float = 30.0


@dataclass(frozen=True)
class OperatorPaths:
    config_path: Path = DEFAULT_OPERATOR_CONFIG_PATH
    run_dir: Path = STATE_DIR / "run"
    log_dir: Path = LOGS_DIR / "operator"


def load_operator_config(path: Path | None = None) -> OperatorConfig:
    target = Path(path) if path is not None else DEFAULT_OPERATOR_CONFIG_PATH
    if not target.is_file():
        return OperatorConfig()
    raw = json.loads(target.read_text(encoding="utf-8"))
    api = raw.get("api") or {}
    dashboard = raw.get("dashboard") or {}
    daemon = raw.get("daemon") or {}
    slack = raw.get("slack_adapter") or {}
    return OperatorConfig(
        api_host=str(api.get("host") or "127.0.0.1"),
        api_port=int(api.get("port") or 8787),
        dashboard_enabled=bool(dashboard.get("enabled", True)),
        dashboard_host=str(dashboard.get("host") or "127.0.0.1"),
        dashboard_port=int(dashboard.get("port") or 5173),
        daemon_enabled=bool(daemon.get("enabled", True)),
        daemon_poll_seconds=float(daemon.get("poll_seconds") or 5),
        slack_enabled=bool(slack.get("enabled", False)),
        slack_poll_seconds=float(slack.get("poll_seconds") or 30),
    )


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, 0, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_file(paths: OperatorPaths, name: str) -> Path:
    return paths.run_dir / f"{name}.pid"


def _error_file(paths: OperatorPaths, name: str) -> Path:
    return paths.run_dir / f"{name}.error"


def read_pid(paths: OperatorPaths, name: str) -> int | None:
    path = _pid_file(paths, name)
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        return None
    return pid if pid > 0 else None


def write_pid(paths: OperatorPaths, name: str, pid: int) -> None:
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    _pid_file(paths, name).write_text(str(pid), encoding="utf-8")
    err = _error_file(paths, name)
    if err.exists():
        err.unlink()


def write_error(paths: OperatorPaths, name: str, message: str) -> None:
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    _error_file(paths, name).write_text(message, encoding="utf-8")


def read_error(paths: OperatorPaths, name: str) -> str | None:
    path = _error_file(paths, name)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def port_is_open(host: str, port: int, *, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _listener_pid_on_port(port: int) -> int | None:
    if sys.platform == "win32":
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            check=False,
        )
        suffix = f":{port}"
        for line in result.stdout.splitlines():
            if "LISTENING" not in line or suffix not in line:
                continue
            parts = line.split()
            try:
                return int(parts[-1])
            except (IndexError, ValueError):
                continue
        return None
    return None


def _release_listener_port(
    host: str,
    port: int,
    *,
    paths: OperatorPaths,
    component: str,
    ctx: ServiceContext | None = None,
) -> None:
    stop_component(component, paths=paths, ctx=ctx)
    time.sleep(0.3)
    if not port_is_open(host, port):
        return
    orphan = _listener_pid_on_port(port)
    if orphan:
        _kill_pid(orphan)
        time.sleep(0.3)


def _item(
    name: str,
    status: str,
    *,
    required: bool,
    detail: str,
    pid: int | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "required": required,
        "detail": detail,
        "pid": pid,
    }


def _api_item(cfg: OperatorConfig) -> dict[str, Any]:
    return _item(
        COMPONENT_API,
        "ok",
        required=True,
        detail=f"control plane answering on {cfg.api_host}:{cfg.api_port}",
    )


def _daemon_item(ctx: ServiceContext, cfg: OperatorConfig) -> dict[str, Any]:
    if not cfg.daemon_enabled:
        return _item(
            COMPONENT_DAEMON,
            "disabled",
            required=False,
            detail="daemon disabled in operator config",
        )
    status = get_daemon_status(ctx.db_path)
    pid = status.pid
    alive = bool(pid and pid_is_alive(pid))
    if status.last_error:
        return _item(
            COMPONENT_DAEMON,
            "error",
            required=True,
            detail=str(status.last_error),
            pid=pid,
        )
    if status.status == "running" and alive:
        return _item(
            COMPONENT_DAEMON,
            "ok",
            required=True,
            detail=f"running pid {pid}",
            pid=pid,
        )
    if status.status == "running" and pid and not alive:
        return _item(
            COMPONENT_DAEMON,
            "stopped",
            required=True,
            detail="daemon heartbeat stale (process not running)",
            pid=pid,
        )
    if status.status == "error":
        return _item(
            COMPONENT_DAEMON,
            "error",
            required=True,
            detail=status.last_error or "daemon reported error",
            pid=pid,
        )
    return _item(
        COMPONENT_DAEMON,
        "stopped",
        required=True,
        detail=f"daemon {status.status}",
        pid=pid,
    )


def _dashboard_item(cfg: OperatorConfig, paths: OperatorPaths) -> dict[str, Any]:
    if not cfg.dashboard_enabled:
        return _item(
            COMPONENT_DASHBOARD,
            "disabled",
            required=False,
            detail="dashboard disabled in operator config",
        )
    if dashboard_is_built() and port_is_open(cfg.api_host, cfg.api_port):
        return _item(
            COMPONENT_DASHBOARD,
            "ok",
            required=True,
            detail=f"served by API on {cfg.api_host}:{cfg.api_port}",
        )
    err = read_error(paths, COMPONENT_DASHBOARD)
    pid = read_pid(paths, COMPONENT_DASHBOARD)
    listening = port_is_open(cfg.dashboard_host, cfg.dashboard_port)
    if err and not listening:
        return _item(
            COMPONENT_DASHBOARD,
            "error",
            required=True,
            detail=err,
            pid=pid,
        )
    if listening:
        return _item(
            COMPONENT_DASHBOARD,
            "ok",
            required=True,
            detail=f"listening on {cfg.dashboard_host}:{cfg.dashboard_port}",
            pid=pid if pid and pid_is_alive(pid) else None,
        )
    return _item(
        COMPONENT_DASHBOARD,
        "stopped",
        required=True,
        detail=f"not listening on {cfg.dashboard_host}:{cfg.dashboard_port}",
        pid=pid if pid and pid_is_alive(pid) else None,
    )


def _slack_item(cfg: OperatorConfig, paths: OperatorPaths) -> dict[str, Any]:
    from projectos.slack_state import public_connection
    from projectos.slack_tokens import token_report

    pid = read_pid(paths, COMPONENT_SLACK)
    alive = bool(pid and pid_is_alive(pid))
    if not cfg.slack_enabled:
        return _item(
            COMPONENT_SLACK,
            "disabled",
            required=False,
            detail="Slack adapter disabled in operator config",
            pid=pid if alive else None,
        )
    tokens = token_report()
    ready = bool(tokens["app_token_present"] and tokens["bot_token_present"])
    info = public_connection(enabled=True, tokens_ready=ready)
    status = str(info.get("status") or "disconnected")
    detail = str(info.get("detail") or "")
    err = read_error(paths, COMPONENT_SLACK)
    if status == "not_configured":
        return _item(
            COMPONENT_SLACK,
            "not_configured",
            required=False,
            detail=detail or "Slack tokens are not set",
            pid=pid if alive else None,
        )
    if err and not alive:
        return _item(
            COMPONENT_SLACK,
            "error",
            required=True,
            detail=err,
            pid=pid,
        )
    if status == "connected":
        return _item(
            COMPONENT_SLACK,
            "connected",
            required=True,
            detail=detail or "Socket Mode connected",
            pid=pid if alive else None,
        )
    if status in {"connecting", "disconnected", "error"}:
        return _item(
            COMPONENT_SLACK,
            status,
            required=True,
            detail=detail or f"Socket Mode {status}",
            pid=pid if alive else None,
        )
    if alive:
        return _item(
            COMPONENT_SLACK,
            "connecting",
            required=True,
            detail=detail or "Slack adapter process is running",
            pid=pid,
        )
    return _item(
        COMPONENT_SLACK,
        "disconnected",
        required=True,
        detail=detail or "Slack adapter is enabled but Socket Mode is not connected",
        pid=pid,
    )


def operator_health(
    ctx: ServiceContext,
    *,
    paths: OperatorPaths | None = None,
    config: OperatorConfig | None = None,
) -> dict[str, Any]:
    paths = paths or OperatorPaths()
    cfg = config or load_operator_config(paths.config_path)
    components = [
        _api_item(cfg),
        _daemon_item(ctx, cfg),
        _dashboard_item(cfg, paths),
        _slack_item(cfg, paths),
    ]
    failed = [
        item
        for item in components
        if item["required"] and item["status"] not in {"ok", "disabled", "connected"}
    ]
    errors = [item for item in failed if item["status"] == "error"]
    if errors:
        overall = "degraded"
        notice = "; ".join(f"{item['name']}: {item['detail']}" for item in errors)
    elif failed:
        overall = "degraded"
        notice = "; ".join(f"{item['name']} {item['status']}" for item in failed)
    else:
        overall = "ok"
        notice = "All enabled operator components are ready."
    return {
        "status": overall,
        "service": "projectos",
        "version": __version__,
        "ready": not failed,
        "notice": notice,
        "components": components,
    }


def _kill_pid(pid: int) -> None:
    if not pid_is_alive(pid):
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    src = str(PROJECTOS_ROOT / "src")
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src if not current else src + os.pathsep + current
    if extra:
        env.update(extra)
    return env


def spawn_logged(
    argv: list[str],
    *,
    name: str,
    paths: OperatorPaths,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> int:
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = paths.log_dir / f"{name}.log"
    log = open(log_path, "a", encoding="utf-8")
    creationflags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS + redirected stdio is unreliable on Windows.
        # BREAKAWAY_FROM_JOB keeps children alive after the starter exits.
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
        )
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd is not None else str(PROJECTOS_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=_child_env(env),
        creationflags=creationflags,
    )
    write_pid(paths, name, int(proc.pid))
    return int(proc.pid)


def stop_component(
    name: str, *, paths: OperatorPaths, ctx: ServiceContext | None = None
) -> None:
    if name == COMPONENT_DAEMON and ctx is not None:
        stop_daemon(ctx.db_path)
    pid = read_pid(paths, name)
    if pid:
        _kill_pid(pid)
    pid_file = _pid_file(paths, name)
    if pid_file.exists():
        pid_file.unlink()


def start_operator(
    ctx: ServiceContext,
    *,
    paths: OperatorPaths | None = None,
    config: OperatorConfig | None = None,
    start_api: bool = True,
    start_daemon: bool = True,
    start_dashboard: bool = True,
    start_slack: bool | None = None,
    wait: bool = False,
) -> dict[str, Any]:
    paths = paths or OperatorPaths()
    cfg = config or load_operator_config(paths.config_path)
    python = sys.executable
    started: dict[str, int] = {}
    if start_api or (start_slack is not False and cfg.slack_enabled):
        try:
            ensure_http_deps()
        except RuntimeError as exc:
            write_error(paths, COMPONENT_API, str(exc))
    if start_dashboard and cfg.dashboard_enabled:
        try:
            ensure_dashboard_built()
        except RuntimeError as exc:
            write_error(paths, COMPONENT_DASHBOARD, f"dashboard build failed: {exc}")
    if start_api:
        _release_listener_port(
            cfg.api_host,
            cfg.api_port,
            paths=paths,
            component=COMPONENT_API,
            ctx=ctx,
        )
        pid = spawn_logged(
            [
                python,
                "-m",
                "projectos",
                "--config",
                str(ctx.registry_path),
                "api",
                "--host",
                cfg.api_host,
                "--port",
                str(cfg.api_port),
                "--db",
                str(ctx.db_path),
            ],
            name=COMPONENT_API,
            paths=paths,
        )
        started[COMPONENT_API] = pid
    if start_daemon and cfg.daemon_enabled:
        pid = spawn_logged(
            [
                python,
                "-m",
                "projectos",
                "--config",
                str(ctx.registry_path),
                "daemon",
                "run",
                "--poll-seconds",
                str(cfg.daemon_poll_seconds),
                "--db",
                str(ctx.db_path),
            ],
            name=COMPONENT_DAEMON,
            paths=paths,
        )
        started[COMPONENT_DAEMON] = pid
    if start_dashboard and cfg.dashboard_enabled and not dashboard_is_built():
        npm = "npm.cmd" if sys.platform == "win32" else "npm"
        try:
            pid = spawn_logged(
                [
                    npm,
                    "run",
                    "dev",
                    "--",
                    "--host",
                    cfg.dashboard_host,
                    "--port",
                    str(cfg.dashboard_port),
                ],
                name=COMPONENT_DASHBOARD,
                paths=paths,
                cwd=PROJECTOS_ROOT / "web",
            )
            started[COMPONENT_DASHBOARD] = pid
        except OSError as exc:
            write_error(paths, COMPONENT_DASHBOARD, f"dashboard failed to start: {exc}")
    slack_wanted = cfg.slack_enabled if start_slack is None else bool(start_slack)
    if slack_wanted:
        if not cfg.slack_enabled:
            write_error(
                paths,
                COMPONENT_SLACK,
                "Slack adapter is not enabled in operator config",
            )
        else:
            pid = spawn_logged(
                [
                    python,
                    "-m",
                    "projectos.slack_adapter",
                    "--config",
                    str(ctx.registry_path),
                    "--db",
                    str(ctx.db_path),
                    "--poll-seconds",
                    str(cfg.slack_poll_seconds),
                ],
                name=COMPONENT_SLACK,
                paths=paths,
            )
            started[COMPONENT_SLACK] = pid
    snapshot = operator_health(ctx, paths=paths, config=cfg)
    if wait:
        try:
            while True:
                time.sleep(1)
                for name, pid in list(started.items()):
                    if pid and not pid_is_alive(pid):
                        raise RuntimeError(f"{name} exited (pid {pid})")
        except (KeyboardInterrupt, RuntimeError):
            stop_operator(ctx, paths=paths)
    return snapshot


def stop_operator(
    ctx: ServiceContext,
    *,
    paths: OperatorPaths | None = None,
) -> dict[str, Any]:
    paths = paths or OperatorPaths()
    for name in (
        COMPONENT_SLACK,
        COMPONENT_DASHBOARD,
        COMPONENT_DAEMON,
        COMPONENT_API,
    ):
        stop_component(name, paths=paths, ctx=ctx)
    cfg = load_operator_config(paths.config_path)
    return operator_health(ctx, paths=paths, config=cfg)
