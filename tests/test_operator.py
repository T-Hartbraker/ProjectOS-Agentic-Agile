"""Local operator packaging reports component readiness without painting failures green."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from projectos.operator import (
    OperatorConfig,
    OperatorPaths,
    load_operator_config,
    operator_health,
    pid_is_alive,
    read_pid,
    spawn_logged,
    stop_component,
)
from projectos.services.context import ServiceContext


def test_load_operator_config_defaults_and_file(tmp_path: Path) -> None:
    missing = load_operator_config(tmp_path / "missing.json")
    assert missing.api_port == 8787
    assert missing.slack_enabled is False
    path = tmp_path / "operator.json"
    path.write_text(
        '{"slack_adapter": {"enabled": true, "poll_seconds": 12}, "daemon": {"enabled": false}}',
        encoding="utf-8",
    )
    cfg = load_operator_config(path)
    assert cfg.slack_enabled is True
    assert cfg.slack_poll_seconds == 12
    assert cfg.daemon_enabled is False


def test_operator_health_is_degraded_when_required_components_are_down(
    tmp_path: Path,
) -> None:
    ctx = ServiceContext(db_path=tmp_path / "projectos.db", registry_path=tmp_path / "projects.json")
    (tmp_path / "projects.json").write_text(
        '{"schema_version": 1, "projects": []}', encoding="utf-8"
    )
    paths = OperatorPaths(
        config_path=tmp_path / "operator.json",
        run_dir=tmp_path / "run",
        log_dir=tmp_path / "logs",
    )
    (tmp_path / "operator.json").write_text(
        '{"api": {"port": 18787}, "daemon": {"enabled": true}, "dashboard": {"enabled": true, "port": 15173}, "slack_adapter": {"enabled": false}}',
        encoding="utf-8",
    )
    snapshot = operator_health(ctx, paths=paths)
    assert snapshot["status"] == "degraded"
    assert snapshot["ready"] is False
    by_name = {item["name"]: item for item in snapshot["components"]}
    assert by_name["api"]["status"] == "ok"
    assert by_name["daemon"]["status"] == "stopped"
    assert by_name["dashboard"]["status"] == "stopped"
    assert by_name["slack_adapter"]["status"] == "disabled"
    assert "daemon" in snapshot["notice"]


def test_operator_health_ok_when_optional_components_are_disabled(tmp_path: Path) -> None:
    ctx = ServiceContext(db_path=tmp_path / "projectos.db", registry_path=tmp_path / "projects.json")
    (tmp_path / "projects.json").write_text(
        '{"schema_version": 1, "projects": []}', encoding="utf-8"
    )
    paths = OperatorPaths(
        config_path=tmp_path / "operator.json",
        run_dir=tmp_path / "run",
        log_dir=tmp_path / "logs",
    )
    (tmp_path / "operator.json").write_text(
        '{"daemon": {"enabled": false}, "dashboard": {"enabled": false}, "slack_adapter": {"enabled": false}}',
        encoding="utf-8",
    )
    snapshot = operator_health(ctx, paths=paths)
    assert snapshot["status"] == "ok"
    assert snapshot["ready"] is True
    by_name = {item["name"]: item for item in snapshot["components"]}
    assert by_name["api"]["status"] == "ok"
    assert by_name["daemon"]["status"] == "disabled"
    assert by_name["dashboard"]["status"] == "disabled"


def test_operator_daemon_uses_lock_pid_when_db_pid_missing(tmp_path: Path, monkeypatch) -> None:
    from projectos.db import connection
    from projectos.migrate import initialize_database

    db_path = tmp_path / "projectos.db"
    initialize_database(db_path)
    lock_path = tmp_path / "projectos.daemon.lock"
    lock_path.write_text("424242", encoding="utf-8")
    with connection(db_path) as conn:
        from projectos.daemon import _persist_daemon

        _persist_daemon(
            conn,
            status="running",
            pid=None,
            heartbeat_at="2026-01-01T00:00:00Z",
            lock_path=str(lock_path),
        )

    def _alive(pid: int) -> bool:
        return pid == 424242

    monkeypatch.setattr("projectos.operator.pid_is_alive", _alive)
    ctx = ServiceContext(db_path=db_path, registry_path=tmp_path / "projects.json")
    (tmp_path / "projects.json").write_text(
        '{"schema_version": 1, "projects": []}', encoding="utf-8"
    )
    paths = OperatorPaths(
        config_path=tmp_path / "operator.json",
        run_dir=tmp_path / "run",
        log_dir=tmp_path / "logs",
    )
    (tmp_path / "operator.json").write_text(
        '{"daemon": {"enabled": true}, "dashboard": {"enabled": false}, "slack_adapter": {"enabled": false}}',
        encoding="utf-8",
    )
    snapshot = operator_health(ctx, paths=paths)
    by_name = {item["name"]: item for item in snapshot["components"]}
    assert by_name["daemon"]["status"] == "ok"
    assert by_name["daemon"]["pid"] == 424242


def test_get_daemon_status_reconciles_missing_pid_from_lock(tmp_path: Path, monkeypatch) -> None:
    from projectos.daemon import get_daemon_status
    from projectos.db import connection
    from projectos.migrate import initialize_database

    db_path = tmp_path / "projectos.db"
    initialize_database(db_path)
    lock_path = tmp_path / "projectos.daemon.lock"
    lock_path.write_text("515151", encoding="utf-8")
    monkeypatch.setattr("projectos.daemon._pid_alive", lambda pid: pid == 515151)
    with connection(db_path) as conn:
        from projectos.daemon import _persist_daemon

        _persist_daemon(
            conn,
            status="running",
            pid=None,
            heartbeat_at="2026-01-01T00:00:00Z",
            lock_path=str(lock_path),
        )
    status = get_daemon_status(db_path)
    assert status.pid == 515151
    with connection(db_path) as conn:
        row = conn.execute("SELECT pid FROM daemon_state WHERE id = 1").fetchone()
    assert row["pid"] == 515151


def test_spawn_and_stop_logged_process(tmp_path: Path) -> None:
    paths = OperatorPaths(
        config_path=tmp_path / "operator.json",
        run_dir=tmp_path / "run",
        log_dir=tmp_path / "logs",
    )
    pid = spawn_logged(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        name="api",
        paths=paths,
        cwd=tmp_path,
    )
    assert pid_is_alive(pid)
    assert read_pid(paths, "api") == pid
    stop_component("api", paths=paths)
    time.sleep(0.3)
    assert not pid_is_alive(pid)
    assert read_pid(paths, "api") is None


def test_slack_adapter_refuses_when_disabled(tmp_path: Path, monkeypatch) -> None:
    from projectos.operator import OperatorConfig
    from projectos.slack_adapter import run_slack_adapter

    ctx = ServiceContext(db_path=tmp_path / "projectos.db", registry_path=tmp_path / "projects.json")
    (tmp_path / "projects.json").write_text(
        '{"schema_version": 1, "projects": []}', encoding="utf-8"
    )
    monkeypatch.setattr(
        "projectos.slack_adapter.load_operator_config",
        lambda: OperatorConfig(slack_enabled=False),
    )
    assert run_slack_adapter(ctx, max_loops=1, require_enabled=True) == 1
    assert run_slack_adapter(ctx, max_loops=1, require_enabled=False) == 0
