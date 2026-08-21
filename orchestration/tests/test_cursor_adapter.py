"""Cursor adapter: unattended argv, timeout, process-tree kill, no QA handoff."""

from __future__ import annotations

import subprocess
import threading
import time
import json
from pathlib import Path

from projectos.cli import main
from projectos.cursor_adapter import (
    build_cursor_command,
    invoke_cursor_agent,
    terminate_process_tree,
)
from projectos.db import connection
from projectos.dispatch import run_dispatch
from projectos.store import (
    active_lease_for_job,
    create_job,
    get_job_by_human_id,
)
from projectos.worker import run_once

from orch_helpers import FakeCompletedProcess, init_git_repo, seed_db, write_registry


def test_unattended_argv_includes_print_and_force() -> None:
    cmd = build_cursor_command(
        agent_bin="agent.cmd",
        prompt="do work",
        workspace=Path("C:/tmp/ws"),
        force=True,
        trust=True,
    )
    assert "--print" in cmd
    assert "--force" in cmd
    assert "--yolo" not in cmd  # prefer --force when both supported
    assert "--trust" in cmd
    assert "--workspace" in cmd
    assert "C:\\tmp\\ws" in cmd or "C:/tmp/ws" in cmd or str(Path("C:/tmp/ws")) in cmd
    assert cmd[-1] == "do work"
    assert "--mode" not in cmd


def test_yolo_alias_available_when_requested() -> None:
    cmd = build_cursor_command(
        agent_bin="agent",
        prompt="x",
        workspace=Path("/ws"),
        force=True,
        use_yolo_alias=True,
    )
    assert "--yolo" in cmd
    assert "--force" not in cmd


def test_successful_subprocess_completion(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    def runner(cmd, **kwargs):
        return FakeCompletedProcess(0, "ok-output", "")

    result = invoke_cursor_agent(
        prompt="hello",
        workspace=ws,
        run_id="ok-run",
        timeout_seconds=5,
        runner=runner,
        output_dir=tmp_path / "out",
    )
    assert result.returncode == 0
    assert result.stdout == "ok-output"
    assert not result.timed_out
    assert "--force" in result.command
    assert "--print" in result.command


def test_never_returning_subprocess_hits_timeout(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    def hanging_runner(cmd, **kwargs):
        time.sleep(30)
        return FakeCompletedProcess(0, "late", "")

    started = time.perf_counter()
    result = invoke_cursor_agent(
        prompt="hang",
        workspace=ws,
        run_id="hang-run",
        timeout_seconds=0.4,
        runner=hanging_runner,
        output_dir=tmp_path / "out",
    )
    assert time.perf_counter() - started < 5.0
    assert result.timed_out
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_cancellation_path(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    cancel = threading.Event()

    def hanging_runner(cmd, **kwargs):
        time.sleep(30)
        return FakeCompletedProcess(0, "late", "")

    def cancel_soon():
        time.sleep(0.15)
        cancel.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.perf_counter()
    result = invoke_cursor_agent(
        prompt="cancel-me",
        workspace=ws,
        run_id="cancel-run",
        timeout_seconds=30,
        runner=hanging_runner,
        cancel_event=cancel,
        output_dir=tmp_path / "out",
    )
    assert time.perf_counter() - started < 5.0
    assert result.cancelled
    assert result.returncode == 130


def test_stdout_stderr_capture(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    def runner(cmd, **kwargs):
        return FakeCompletedProcess(0, "OUT_LINE", "ERR_LINE")

    result = invoke_cursor_agent(
        prompt="cap",
        workspace=ws,
        run_id="cap-run",
        runner=runner,
        output_dir=tmp_path / "out",
    )
    assert "OUT_LINE" in result.stdout
    assert "ERR_LINE" in result.stderr
    assert Path(result.stdout_ref).read_text(encoding="utf-8") == "OUT_LINE"
    assert "ERR_LINE" in Path(result.stderr_ref).read_text(encoding="utf-8")


def test_process_tree_termination_on_timeout(tmp_path: Path) -> None:
    """terminate_process_tree kills a hanging child process."""
    import sys

    ws = tmp_path / "ws"
    ws.mkdir()
    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(ws),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.2)
    assert proc.poll() is None
    started = time.perf_counter()
    terminate_process_tree(proc.pid)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("process tree was not terminated")
    assert time.perf_counter() - started < 8.0
    assert proc.poll() is not None


def test_popen_path_timeout_kills_real_process(tmp_path: Path) -> None:
    """Production _run_via_popen path enforces timeout against a real process."""
    import sys

    ws = tmp_path / "ws"
    ws.mkdir()
    hang = tmp_path / "hang_agent.py"
    hang.write_text(
        "import sys, time\n"
        "# mimic agent.cmd absorbing argv then hanging\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    # Build a fake agent.cmd that just runs our hang script.
    if sys.platform == "win32":
        agent = tmp_path / "fake-agent.cmd"
        agent.write_text(
            f'@echo off\n"{sys.executable}" "{hang}" %*\n',
            encoding="utf-8",
        )
    else:
        agent = tmp_path / "fake-agent"
        agent.write_text(
            f"#!/bin/sh\nexec '{sys.executable}' '{hang}' \"$@\"\n",
            encoding="utf-8",
        )
        agent.chmod(0o755)

    started = time.perf_counter()
    result = invoke_cursor_agent(
        prompt="PROJECTOS_HEADLESS_OK",
        workspace=ws,
        run_id="popen-timeout",
        timeout_seconds=1.0,
        agent_bin=str(agent),
        output_dir=tmp_path / "out",
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 8.0
    assert result.timed_out
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_no_qa_handoff_after_cursor_timeout(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-003",
                "repository_root": str(repo.resolve()),
                "enabled": True,
            }
        ],
    )
    db = seed_db(tmp_path / "projectos.db")

    def hanging_runner(cmd, **kwargs):
        time.sleep(30)
        return FakeCompletedProcess(0, "late", "")

    with connection(db) as conn:
        create_job(
            conn,
            human_id="JOB-DEL-TO",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            requires_worktree=True,
            base_git_sha="base",
            assignment={
                "requirement_ref": "story:US-007",
                "title": "t",
                "acceptance_criteria": ["AC-1: x"],
            },
            work_item_type="story",
            work_item_human_id="US-007",
        )

    result = run_once(
        db_path=db,
        registry_path=cfg,
        job_human_id="JOB-DEL-TO",
        cursor_runner=hanging_runner,
        skip_identity_validation=True,
        timeout_seconds=0.4,
    )
    assert result.exit_code != 0
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-DEL-TO")
        assert job.status != "SUCCEEDED"
        assert job.status != "RUNNING"
        assert active_lease_for_job(conn, job.id) is None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM qa_evidence WHERE delivery_job_id = ?",
                (job.id,),
            ).fetchone()[0]
            == 0
        )


def test_dispatcher_does_not_wait_forever_for_timed_out_worker(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-003",
                "repository_root": str(repo.resolve()),
                "enabled": True,
            }
        ],
    )
    db = seed_db(tmp_path / "projectos.db")

    def hanging_runner(cmd, **kwargs):
        time.sleep(60)
        return FakeCompletedProcess(0, "late", "")

    with connection(db) as conn:
        create_job(
            conn,
            human_id="JOB-DISP-TO",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="PM",
            queue="PM",
            status="READY",
        )

    started = time.perf_counter()
    result = run_dispatch(
        once=True,
        max_parallel=1,
        db_path=db,
        registry_path=cfg,
        cursor_runner=hanging_runner,
        skip_identity_validation=True,
        timeout_seconds=0.5,
        wait_cycle_seconds=0.2,
        shutdown_grace_seconds=2.0,
    )
    assert time.perf_counter() - started < 10.0
    assert any(r.job_human_id == "JOB-DISP-TO" for r in result.completed)


def test_cursor_smoke_help() -> None:
    assert main(["cursor", "--help"]) == 0
    assert main(["cursor", "smoke", "--help"]) == 0


def _cursor_ok(
    *,
    stdout: str,
    returncode: int = 0,
    timed_out: bool = False,
    cancelled: bool = False,
    command: list[str] | None = None,
) -> CursorRunResult:
    from projectos.cursor_adapter import CursorRunResult

    return CursorRunResult(
        command=command
        or [
            "agent",
            "--print",
            "--output-format",
            "json",
            "--trust",
            "--force",
            "--workspace",
            "C:/ws",
            "prompt",
        ],
        returncode=returncode,
        stdout=stdout,
        stderr="",
        started_at="t0",
        ended_at="t1",
        duration_ms=10,
        output_ref="meta.json",
        stdout_ref="stdout.txt",
        stderr_ref="stderr.txt",
        prompt_ref="prompt.txt",
        workspace=Path("C:/ws"),
        worktree_name=None,
        usage={"status": "unknown"},
        timed_out=timed_out,
        cancelled=cancelled,
    )


def test_json_smoke_success(tmp_path: Path) -> None:
    from projectos.cursor_adapter import evaluate_cursor_smoke

    envelope = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "PROJECTOS_HEADLESS_OK",
            "session_id": "s1",
        }
    )
    smoke = evaluate_cursor_smoke(
        _cursor_ok(stdout=envelope),
        evidence_dir=tmp_path / "ev",
    )
    assert smoke.smoke_ok
    assert smoke.result_text == "PROJECTOS_HEADLESS_OK"
    assert smoke.parsed_ref
    assert Path(smoke.parsed_ref).is_file()


def test_json_smoke_wrong_result_text(tmp_path: Path) -> None:
    from projectos.cursor_adapter import evaluate_cursor_smoke

    envelope = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "NOT_THE_TOKEN",
        }
    )
    smoke = evaluate_cursor_smoke(
        _cursor_ok(stdout=envelope),
        evidence_dir=tmp_path / "ev",
    )
    assert not smoke.smoke_ok
    assert smoke.reason == "wrong_result_text"


def test_json_smoke_empty_output(tmp_path: Path) -> None:
    from projectos.cursor_adapter import evaluate_cursor_smoke

    smoke = evaluate_cursor_smoke(
        _cursor_ok(stdout=""),
        evidence_dir=tmp_path / "ev",
    )
    assert not smoke.smoke_ok
    assert smoke.reason == "empty_stdout"


def test_json_smoke_malformed_json(tmp_path: Path) -> None:
    from projectos.cursor_adapter import evaluate_cursor_smoke

    smoke = evaluate_cursor_smoke(
        _cursor_ok(stdout="not-json {"),
        evidence_dir=tmp_path / "ev",
    )
    assert not smoke.smoke_ok
    assert smoke.reason == "malformed_json"


def test_json_smoke_nonzero_return(tmp_path: Path) -> None:
    from projectos.cursor_adapter import evaluate_cursor_smoke

    envelope = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "PROJECTOS_HEADLESS_OK",
        }
    )
    smoke = evaluate_cursor_smoke(
        _cursor_ok(stdout=envelope, returncode=1),
        evidence_dir=tmp_path / "ev",
    )
    assert not smoke.smoke_ok
    assert smoke.reason.startswith("nonzero_returncode")


def test_json_smoke_timeout(tmp_path: Path) -> None:
    from projectos.cursor_adapter import evaluate_cursor_smoke

    smoke = evaluate_cursor_smoke(
        _cursor_ok(stdout="", timed_out=True, returncode=124),
        evidence_dir=tmp_path / "ev",
    )
    assert not smoke.smoke_ok
    assert smoke.reason == "timed_out"


def test_run_cursor_smoke_uses_json_format(tmp_path: Path) -> None:
    from projectos.cursor_adapter import run_cursor_smoke_test

    ws = tmp_path / "ws"
    ws.mkdir()
    envelope = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "PROJECTOS_HEADLESS_OK",
        }
    )

    def runner(cmd, **kwargs):
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert "--print" in cmd
        assert "--force" in cmd
        return FakeCompletedProcess(0, envelope, "")

    smoke = run_cursor_smoke_test(
        workspace=ws,
        timeout_seconds=5,
        output_dir=tmp_path / "out",
        runner=runner,
    )
    assert smoke.smoke_ok
    assert "--output-format" in smoke.cursor.command
    assert "json" in smoke.cursor.command
