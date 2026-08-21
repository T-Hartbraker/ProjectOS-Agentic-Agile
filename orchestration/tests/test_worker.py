"""Worker runtime and lease lifecycle tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from projectos.cli import main
from projectos.db import connection
from projectos.store import (
    active_lease_for_job,
    create_job,
    get_job,
    get_job_by_human_id,
    recover_expired_leases,
    acquire_lease,
)
from projectos.worker import run_once

from orch_helpers import (
    add_ready_job,
    init_git_repo,
    make_cursor_runner,
    seed_db,
    write_registry,
)


def _cfg(tmp_path: Path, repo: Path) -> Path:
    return write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-003",
                "repository_root": str(repo.resolve()),
                "enabled": True,
            }
        ],
    )


def test_worker_help() -> None:
    assert main(["worker", "--help"]) == 0


def test_successful_job_execution(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    add_ready_job(db, repository_root=repo, agent_role="PM", queue="PM")

    result = run_once(
        db_path=db,
        registry_path=cfg,
        cursor_runner=make_cursor_runner(returncode=0),
        skip_identity_validation=True,
    )
    assert result.status == "succeeded"
    assert result.exit_code == 0

    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-001")
        assert job is not None
        assert job.status == "SUCCEEDED"
        assert active_lease_for_job(conn, job.id) is None
        run = conn.execute(
            "SELECT * FROM agent_runs WHERE job_id = ?", (job.id,)
        ).fetchone()
        assert run is not None
        assert int(run["exit_code"]) == 0
        assert run["started_at"]
        assert run["ended_at"]
        assert run["duration_ms"] is not None
        assert run["output_ref"]


def test_no_ready_jobs(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)

    result = run_once(
        db_path=db,
        registry_path=cfg,
        cursor_runner=make_cursor_runner(),
        skip_identity_validation=True,
    )
    assert result.status == "idle"
    assert result.exit_code == 0
    assert "No READY" in result.message


def test_lease_acquisition(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    job_id = add_ready_job(db, repository_root=repo)

    with connection(db) as conn:
        job = get_job(conn, job_id)
        lease = acquire_lease(conn, job, worker_id="worker-a", lease_seconds=60)
        assert lease.worker_id == "worker-a"
        refreshed = get_job(conn, job_id)
        assert refreshed.status == "LEASED"
        assert active_lease_for_job(conn, job_id) is not None

        # Second acquire against non-READY must fail.
        try:
            acquire_lease(conn, refreshed, worker_id="worker-b", lease_seconds=60)
            assert False, "expected LeaseError"
        except Exception as exc:
            assert "not READY" in str(exc)


def test_expired_lease_recovery(tmp_path: Path) -> None:
    """LEASED (never started) expired lease returns to READY without burning attempt."""
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    job_id = add_ready_job(db, repository_root=repo)

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")

    with connection(db) as conn:
        conn.execute(
            """
            UPDATE orchestration_jobs
            SET status = 'LEASED', started_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (past, job_id),
        )
        conn.execute(
            """
            INSERT INTO worker_leases (job_id, worker_id, leased_at, expires_at)
            VALUES (?, 'dead-worker', ?, ?)
            """,
            (job_id, past, past),
        )
        recovered = recover_expired_leases(conn)
        assert job_id in recovered
        job = get_job(conn, job_id)
        assert job.status == "READY"
        assert job.attempt == 0
        assert active_lease_for_job(conn, job_id) is None

    # After recovery, worker can execute the job.
    cfg = _cfg(tmp_path, repo)
    result = run_once(
        db_path=db,
        registry_path=cfg,
        job_human_id="JOB-001",
        cursor_runner=make_cursor_runner(returncode=0),
        skip_identity_validation=True,
    )
    assert result.status == "succeeded"


def test_cursor_nonzero_exit(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    add_ready_job(db, repository_root=repo, max_attempts=3)

    result = run_once(
        db_path=db,
        registry_path=cfg,
        cursor_runner=make_cursor_runner(returncode=2, stderr="boom"),
        skip_identity_validation=True,
    )
    assert result.exit_code == 1
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-001")
        assert job is not None
        assert job.status == "RETRY_WAIT"
        assert job.attempt == 1
        assert active_lease_for_job(conn, job.id) is None


def test_timeout(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    add_ready_job(db, repository_root=repo)

    result = run_once(
        db_path=db,
        registry_path=cfg,
        timeout_seconds=1.0,
        cursor_runner=make_cursor_runner(timeout=True, timeout_seconds=1.0),
        skip_identity_validation=True,
    )
    assert result.exit_code == 1
    assert "timed out" in result.message.lower()
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-001")
        assert job is not None
        assert job.status == "RETRY_WAIT"
        run = conn.execute(
            "SELECT exit_code FROM agent_runs WHERE job_id = ?", (job.id,)
        ).fetchone()
        assert int(run["exit_code"]) == 124


def test_retry_exhaustion(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    add_ready_job(db, repository_root=repo, max_attempts=1)

    result = run_once(
        db_path=db,
        registry_path=cfg,
        cursor_runner=make_cursor_runner(returncode=1, stderr="fail"),
        skip_identity_validation=True,
    )
    assert result.exit_code == 1
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-001")
        assert job is not None
        assert job.status == "FAILED"
        assert job.attempt == 1
        assert active_lease_for_job(conn, job.id) is None


def test_lease_release_after_failure(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    add_ready_job(db, repository_root=repo)

    result = run_once(
        db_path=db,
        registry_path=cfg,
        cursor_runner=make_cursor_runner(returncode=99),
        skip_identity_validation=True,
    )
    assert result.exit_code == 1
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-001")
        assert job is not None
        assert active_lease_for_job(conn, job.id) is None
        lease = conn.execute(
            "SELECT * FROM worker_leases WHERE job_id = ?", (job.id,)
        ).fetchone()
        assert lease is not None
        assert lease["released_at"] is not None
        assert "failure" in str(lease["release_reason"])


def test_worktree_collision_refusal(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    wt_name = "PRJ-003__JOB-COLLIDE"

    with connection(db) as conn:
        holder = create_job(
            conn,
            human_id="JOB-HOLD",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="RUNNING",
            requires_worktree=True,
            worktree_name=wt_name,
            worktree_path=str(tmp_path / "holder-wt"),
        )
        assert holder.status == "RUNNING"
        create_job(
            conn,
            human_id="JOB-NEW",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            requires_worktree=True,
            worktree_name=wt_name,
        )

    result = run_once(
        db_path=db,
        registry_path=cfg,
        job_human_id="JOB-NEW",
        cursor_runner=make_cursor_runner(returncode=0),
        skip_identity_validation=True,
    )
    assert result.exit_code == 1
    assert result.status == "blocked"
    assert "already claimed" in result.message.lower()

    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-NEW")
        assert job is not None
        assert job.status == "BLOCKED"
        assert active_lease_for_job(conn, job.id) is None
        holder_job = get_job_by_human_id(conn, "JOB-HOLD")
        assert holder_job is not None
        assert holder_job.status == "RUNNING"


def test_worker_once_cli(tmp_path: Path, capsys) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    # No jobs -> idle via CLI
    code = main(
        [
            "--config",
            str(cfg),
            "worker",
            "--once",
            "--db",
            str(db),
        ]
    )
    # CLI will try real identity validation path only when a job exists.
    # With no jobs, run_once returns idle before validation.
    assert code == 0
    out = capsys.readouterr().out
    assert "idle" in out or "No READY" in out
