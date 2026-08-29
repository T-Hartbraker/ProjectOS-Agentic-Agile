"""True execution concurrency regression tests."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.errors import OrchestrationError
from projectos.migrate import initialize_database
from projectos.store import acquire_lease, create_job, get_job


def _ctx(tmp_path: Path):
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    repo_root = str(repo.resolve())
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": repo_root, "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return db, repo_root


def test_concurrent_job_claim_exactly_one_owner(tmp_path: Path) -> None:
    db, repo_root = _ctx(tmp_path)
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-RACE-REAL",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
        )
        job_id = job.id

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[Exception] = []

    def contender(worker_id: str) -> None:
        try:
            barrier.wait(timeout=5)
            with connection(db) as conn:
                acquire_lease(conn, get_job(conn, job_id), worker_id=worker_id, lease_seconds=60)
            results.append(worker_id)
        except OrchestrationError as exc:
            errors.append(exc)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=contender, args=("worker-a",))
    t2 = threading.Thread(target=contender, args=("worker-b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert len(results) == 1
    assert len(errors) == 1
    with connection(db) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM worker_leases WHERE job_id = ? AND released_at IS NULL",
            (job_id,),
        ).fetchone()[0]
        started = conn.execute(
            "SELECT COUNT(*) FROM projectos_events WHERE event_type = 'WORK_STARTED'"
        ).fetchone()[0]
    assert int(row) == 1
    assert int(started) == 0
