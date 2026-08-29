"""Dispatch, recovery, pause, and daemon HTTP APIs."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.http import create_app
from projectos.migrate import initialize_database
from projectos.store import acquire_operation_lock, create_job, get_job_by_human_id

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from orch_helpers import make_cursor_runner


def _repo(tmp_path: Path, name: str, human_id: str) -> Path:
    repo = init_git_repo(tmp_path / name)
    write_identity(repo, project_human_id=human_id, project_name="Example")
    (repo / "project-control").mkdir(parents=True, exist_ok=True)
    (repo / "project-control" / "project.db").write_bytes(b"sqlite")
    return repo


def _client(tmp_path: Path, human_id: str = "PRJ-A") -> TestClient:
    app = create_app(
        registry_path=tmp_path / "projects.json",
        db_path=tmp_path / "projectos.db",
        projectctl_runner=lambda root: fake_status(human_id),
        cursor_runner=make_cursor_runner(returncode=0),
        skip_identity_validation=True,
    )
    return TestClient(app)


def _register(client: TestClient, repo: Path) -> None:
    created = client.post("/v1/projects", json={"repository_path": str(repo.resolve())})
    assert created.status_code == 201, created.text


def _ready_job(tmp_path: Path, repo: Path, *, human_id: str, project: str) -> None:
    initialize_database(tmp_path / "projectos.db")
    with connection(tmp_path / "projectos.db") as conn:
        create_job(
            conn,
            human_id=human_id,
            project_human_id=project,
            repository_root=repo,
            agent_role="PM",
            queue="PM",
            status="READY",
        )


def test_run_once_pause_resume_and_idle(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "alpha", "PRJ-A")
    client = _client(tmp_path)
    _register(client, repo)
    _ready_job(tmp_path, repo, human_id="JOB-A", project="PRJ-A")

    eligible = client.get("/v1/projects/PRJ-A/dispatch/eligible")
    assert eligible.status_code == 200
    assert {j["human_id"] for j in eligible.json()["jobs"]} == {"JOB-A"}

    paused = client.post(
        "/v1/projects/PRJ-A/orchestration/pause",
        json={"reason": "operator hold"},
    )
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert client.get("/v1/projects/PRJ-A/dispatch/eligible").json()["jobs"] == []
    blocked = client.post("/v1/projects/PRJ-A/dispatch/run-once")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "conflict"

    resumed = client.post("/v1/projects/PRJ-A/orchestration/resume")
    assert resumed.json()["paused"] is False
    ran = client.post("/v1/projects/PRJ-A/dispatch/run-once")
    assert ran.status_code == 200, ran.text
    assert ran.json()["mode"] == "once"
    assert ran.json()["completed"][0]["job_human_id"] == "JOB-A"

    idle = client.post("/v1/projects/PRJ-A/dispatch/run-until-idle")
    assert idle.status_code == 200
    assert idle.json()["completed"] == [] or idle.json()["mode"] in {"once", "until-idle"}


def test_idempotency_lock_recovery_daemon_and_path_rejection(tmp_path: Path) -> None:
    repo_a = _repo(tmp_path, "alpha", "PRJ-A")
    repo_b = _repo(tmp_path, "bravo", "PRJ-B")
    write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-A",
                "repository_root": str(repo_a.resolve()),
                "enabled": True,
            },
            {
                "project_human_id": "PRJ-B",
                "repository_root": str(repo_b.resolve()),
                "enabled": True,
            },
        ],
    )
    client = _client(tmp_path)
    _ready_job(tmp_path, repo_a, human_id="JOB-A", project="PRJ-A")
    _ready_job(tmp_path, repo_b, human_id="JOB-B", project="PRJ-B")

    first = client.post(
        "/v1/projects/PRJ-A/dispatch/run-once",
        json={"idempotency_key": "run-1"},
    )
    assert first.status_code == 200
    replay = client.post(
        "/v1/projects/PRJ-A/dispatch/run-once",
        json={"idempotency_key": "run-1"},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    mismatch = client.post(
        "/v1/projects/PRJ-A/dispatch/run-once",
        json={"idempotency_key": "run-1", "job_human_id": "JOB-A"},
    )
    assert mismatch.status_code == 409

    foreign = client.post(
        "/v1/projects/PRJ-A/dispatch/run-once",
        json={"job_human_id": "JOB-B"},
    )
    assert foreign.status_code == 404

    rejected = client.post(
        "/v1/projects/PRJ-A/dispatch/run-once",
        json={"repository_root": str(repo_a), "command": "rm -rf /"},
    )
    assert rejected.status_code == 422

    preview = client.get("/v1/projects/PRJ-A/recovery/preview")
    assert preview.status_code == 200
    assert preview.json()["dry_run"] is True
    with connection(tmp_path / "projectos.db") as conn:
        job = get_job_by_human_id(conn, "JOB-A")
        assert job is not None
        status_before = job.status
    executed = client.post(
        "/v1/projects/PRJ-A/recovery/execute",
        headers={"Idempotency-Key": "rec-1"},
    )
    assert executed.status_code == 200
    assert executed.json()["dry_run"] is False
    with connection(tmp_path / "projectos.db") as conn:
        job = get_job_by_human_id(conn, "JOB-A")
        assert job is not None
        assert job.status == status_before

    initialize_database(tmp_path / "projectos.db")
    with connection(tmp_path / "projectos.db") as conn:
        acquire_operation_lock(conn, "project:PRJ-B:control", owner="tester")
    busy = client.post("/v1/projects/PRJ-B/dispatch/run-once")
    assert busy.status_code == 409

    daemon = client.get("/v1/daemon")
    assert daemon.status_code == 200
    assert "status" in daemon.json()
    assert "pid" in daemon.json()
    scheduler = client.get("/v1/scheduler")
    assert scheduler.status_code == 200
    assert "daemon" in scheduler.json()
    assert "schedules" in scheduler.json()
