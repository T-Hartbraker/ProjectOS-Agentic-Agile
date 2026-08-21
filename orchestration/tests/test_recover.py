"""Recovery command tests: interrupted worker and identity drift."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projectos.cli import main
from projectos.db import connection
from projectos.projectctl_bridge import ProjectctlStatusResult
from projectos.recover import run_recovery
from projectos.store import (
    active_lease_for_job,
    create_job,
    get_job,
    get_job_by_human_id,
)
from projectos.worktree import ensure_worktree

from orch_helpers import init_git_repo, seed_db, write_registry


def _past_iso(minutes: int = 5) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(minutes=minutes))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_identity(repo: Path, project_human_id: str = "PRJ-003") -> Path:
    project_dir = repo / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / "repository.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository_type": "delivery-project",
                "project_human_id": project_human_id,
                "project_name": "Example",
                "isolation_model": "one-project-per-repository",
                "orchestration_scope": "project",
                "cross_project_access": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _fake_status(human_id: str) -> ProjectctlStatusResult:
    return ProjectctlStatusResult(
        returncode=0,
        stdout=f"Active project: {human_id} - Example\n",
        stderr="",
        active_project_human_id=human_id,
        python_executable=Path("/fake/python"),
    )


def test_recover_help() -> None:
    assert main(["recover", "--help"]) == 0


def test_interrupted_worker_recovery(tmp_path: Path) -> None:
    """RUNNING + expired lease → RETRY_WAIT (policy) → READY after recover."""
    repo = init_git_repo(tmp_path / "repo")
    _write_identity(repo)
    db = seed_db(tmp_path / "projectos.db")
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
    candidate = "abc123candidate"
    base = "def456base"
    past = _past_iso()

    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-INT",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            max_attempts=3,
            requires_worktree=True,
            worktree_name="PRJ-003__JOB-INT",
            worktree_path=str(tmp_path / "missing-wt"),
            base_git_sha=base,
            identity_snapshot={
                "project_human_id": "PRJ-003",
                "repository_root": str(repo.resolve()),
            },
        )
        conn.execute(
            """
            UPDATE orchestration_jobs
            SET status = 'RUNNING',
                started_at = ?,
                candidate_git_sha = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (past, candidate, past, job.id),
        )
        conn.execute(
            """
            INSERT INTO worker_leases (job_id, worker_id, leased_at, expires_at)
            VALUES (?, 'crashed-worker', ?, ?)
            """,
            (job.id, past, past),
        )

    report = run_recovery(
        db_path=db,
        registry_path=cfg,
        projectctl_runner=lambda root: _fake_status("PRJ-003"),
        promote_retry_wait=True,
    )

    assert job.id in report.expired_lease_job_ids
    assert "JOB-INT" in report.promoted_ready

    with connection(db) as conn:
        refreshed = get_job_by_human_id(conn, "JOB-INT")
        assert refreshed is not None
        assert refreshed.status == "READY"
        assert refreshed.attempt == 1
        assert refreshed.candidate_git_sha == candidate
        assert refreshed.base_git_sha == base
        assert Path(refreshed.repository_root).resolve() == repo.resolve()
        assert refreshed.worktree_name == "PRJ-003__JOB-INT"
        assert active_lease_for_job(conn, refreshed.id) is None


def test_identity_drift_recovery(tmp_path: Path) -> None:
    """Identity drift blocks jobs without moving repository or clearing candidate."""
    repo = init_git_repo(tmp_path / "repo")
    other = init_git_repo(tmp_path / "other-repo")
    _write_identity(repo, "PRJ-003")
    _write_identity(other, "PRJ-003")
    db = seed_db(tmp_path / "projectos.db")
    # Registry now points at a different repository than the job binding.
    cfg = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-003",
                "repository_root": str(other.resolve()),
                "enabled": True,
            }
        ],
    )
    candidate = "cand-sha-keep"
    past = _past_iso()

    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-DRIFT",
            project_human_id="PRJ-003",
            repository_root=repo,  # bound to original repo
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            base_git_sha="base-keep",
            identity_snapshot={
                "project_human_id": "PRJ-003",
                "repository_root": str(repo.resolve()),
            },
        )
        conn.execute(
            """
            UPDATE orchestration_jobs
            SET status = 'RUNNING',
                started_at = ?,
                candidate_git_sha = ?,
                worktree_name = 'PRJ-003__JOB-DRIFT',
                worktree_path = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (past, candidate, str(tmp_path / "wt"), past, job.id),
        )
        conn.execute(
            """
            INSERT INTO worker_leases (job_id, worker_id, leased_at, expires_at)
            VALUES (?, 'dead', ?, ?)
            """,
            (job.id, past, past),
        )
        bound_root = str(Path(job.repository_root).resolve())

    report = run_recovery(
        db_path=db,
        registry_path=cfg,
        projectctl_runner=lambda root: _fake_status("PRJ-003"),
    )

    assert report.ok is False
    assert "JOB-DRIFT" in report.blocked
    assert any(not c.ok for c in report.identity_checks)

    with connection(db) as conn:
        refreshed = get_job(conn, job.id)
        assert refreshed.status == "BLOCKED"
        assert Path(refreshed.repository_root).resolve() == Path(bound_root)
        assert refreshed.candidate_git_sha == candidate
        assert refreshed.base_git_sha == "base-keep"
        assert refreshed.worktree_name == "PRJ-003__JOB-DRIFT"
        # Must not have been rewritten to the drifted registry root.
        assert Path(refreshed.repository_root).resolve() != other.resolve()
        assert active_lease_for_job(conn, refreshed.id) is None


def test_recover_never_adopts_unknown_worktree(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    _write_identity(repo)
    db = seed_db(tmp_path / "projectos.db")
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
    # Create a real git worktree that no job records.
    orphan = ensure_worktree(repo, name="orphan-untracked")
    with connection(db) as conn:
        create_job(
            conn,
            human_id="JOB-OK",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="PM",
            queue="PM",
            status="READY",
        )

    report = run_recovery(
        db_path=db,
        registry_path=cfg,
        projectctl_runner=lambda root: _fake_status("PRJ-003"),
    )
    assert str(orphan.path) in report.unknown_worktrees_ignored or any(
        str(orphan.path) == p or orphan.path.name in p
        for p in report.unknown_worktrees_ignored
    )
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-OK")
        assert job is not None
        assert job.worktree_path is None
        assert job.worktree_name is None
        assert job.status == "READY"


def test_recover_cli(tmp_path: Path, capsys) -> None:
    repo = init_git_repo(tmp_path / "repo")
    _write_identity(repo)
    db = seed_db(tmp_path / "projectos.db")
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
    # Patch validation via monkeypatch would be needed for live CLI;
    # exercise help + empty recover through run_recovery path above.
    # CLI without jobs should still succeed identity-vacuously.
    code = main(["--config", str(cfg), "recover", "--db", str(db)])
    out = capsys.readouterr().out
    assert "expired_leases:" in out
    # No active jobs → ok with zero identity checks
    assert code == 0
