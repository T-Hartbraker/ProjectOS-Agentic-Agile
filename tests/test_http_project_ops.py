"""Project-scoped status, plan, and job HTTP APIs."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.store import append_run_event, get_job_by_human_id

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from projectos.http import create_app


def _repo(tmp_path: Path, name: str, human_id: str) -> Path:
    repo = init_git_repo(tmp_path / name)
    write_identity(repo, project_human_id=human_id, project_name="Example")
    return repo


def _client(tmp_path: Path, human_id: str = "PRJ-A") -> TestClient:
    app = create_app(
        registry_path=tmp_path / "projects.json",
        db_path=tmp_path / "projectos.db",
        projectctl_runner=lambda root: fake_status(human_id),
    )
    return TestClient(app)


def _plan(project: str, *, iteration: str | None = "ITER-1") -> dict:
    return {
        "schema_version": 1,
        "project_human_id": project,
        "sponsor_authority": "approved",
        "iteration_human_id": iteration,
        "jobs": [
            {
                "human_id": "JOB-A",
                "queue": "PM",
                "agent_role": "PM",
            },
            {
                "human_id": "JOB-B",
                "queue": "PM",
                "agent_role": "PM",
                "depends_on": ["JOB-A"],
            },
        ],
    }


def test_summary_plan_jobs_graph_eligibility_events_and_replay(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "alpha", "PRJ-A")
    client = _client(tmp_path, "PRJ-A")
    assert (
        client.post("/v1/projects", json={"repository_path": str(repo.resolve())}).status_code
        == 201
    )

    summary = client.get("/v1/projects/PRJ-A/summary")
    assert summary.status_code == 200
    assert summary.json()["project_human_id"] == "PRJ-A"
    assert summary.json()["has_accepted_plan"] is False
    current = client.get("/v1/projects/PRJ-A/current")
    assert current.status_code == 200
    assert current.json()["iteration_human_id"] is None
    assert "repository_root" not in current.json()

    accepted = client.post(
        "/v1/projects/PRJ-A/plan/accept",
        json={"plan": _plan("PRJ-A"), "iteration_human_id": "ITER-1"},
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["status"] == "accepted"
    assert body["jobs_created"] == ["JOB-A", "JOB-B"]
    assert body["plan_source"] == "override"
    assert "repository_root" not in body

    jobs = client.get("/v1/projects/PRJ-A/jobs")
    assert jobs.status_code == 200
    ids = {j["human_id"] for j in jobs.json()["jobs"]}
    assert ids == {"JOB-A", "JOB-B"}
    detail = client.get("/v1/projects/PRJ-A/jobs/JOB-A")
    assert detail.status_code == 200
    assert detail.json()["status"] == "READY"
    assert detail.json()["queue"] == "PM"
    assert detail.json()["presentation"]["queue_label"] == "Planning"
    assert detail.json()["presentation"]["status_label"] == "Ready"
    assert "repository_root" not in detail.json()

    graph = client.get("/v1/projects/PRJ-A/graph")
    assert graph.status_code == 200
    edges = graph.json()["edges"]
    assert {"job_human_id": "JOB-B", "depends_on": "JOB-A"} in edges

    eligible = client.get("/v1/projects/PRJ-A/dispatch/eligible")
    assert eligible.status_code == 200
    eligible_ids = {j["human_id"] for j in eligible.json()["jobs"]}
    assert "JOB-A" in eligible_ids
    assert "JOB-B" not in eligible_ids

    initialize_database(tmp_path / "projectos.db")
    with connection(tmp_path / "projectos.db") as conn:
        job = get_job_by_human_id(conn, "JOB-A")
        assert job is not None
        append_run_event(conn, job.id, "job.ready", status="READY", message="seed")
    events = client.get("/v1/projects/PRJ-A/events")
    assert events.status_code == 200
    assert events.json()["events"][0]["job_human_id"] == "JOB-A"

    current = client.get("/v1/projects/PRJ-A/current")
    assert current.json()["iteration_human_id"] == "ITER-1"
    assert current.json()["from_accepted_plan"] is True

    dry = client.post("/v1/projects/PRJ-A/plan/dry-run", json={})
    assert dry.status_code == 200
    assert dry.json()["status"] == "dry_run"
    assert dry.json()["plan_source"] == "accepted_replay"
    assert dry.json()["jobs_created"] == []
    after = client.get("/v1/projects/PRJ-A/jobs")
    assert {j["human_id"] for j in after.json()["jobs"]} == {"JOB-A", "JOB-B"}


def test_job_scoped_to_project_and_repository_root_rejected(tmp_path: Path) -> None:
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
    client = create_app(
        registry_path=tmp_path / "projects.json",
        db_path=tmp_path / "projectos.db",
        projectctl_runner=lambda root: fake_status("PRJ-A"),
    )
    # projectctl_runner is fixed to PRJ-A; register B via existing registry rows.
    http = TestClient(client)
    accept = http.post(
        "/v1/projects/PRJ-A/plan/accept",
        json={"plan": _plan("PRJ-A")},
    )
    assert accept.status_code == 200
    missing = http.get("/v1/projects/PRJ-B/jobs/JOB-A")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"

    rejected = http.post(
        "/v1/projects/PRJ-A/plan/dry-run",
        json={"plan": {"repository_root": str(repo_b.resolve())}},
    )
    assert rejected.status_code == 422
    extra = http.post(
        "/v1/projects/PRJ-A/plan/accept",
        json={"repository_root": str(repo_a.resolve()), "plan": _plan("PRJ-A")},
    )
    assert extra.status_code == 422
