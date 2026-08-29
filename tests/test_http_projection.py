"""Project projection: polled read model without orchestration tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.http import create_app
from projectos.migrate import initialize_database
from projectos.store import (
    append_run_event,
    create_job,
    get_job_by_human_id,
    insert_qa_evidence,
    set_project_paused,
)

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


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


def _plan(project: str) -> dict:
    return {
        "schema_version": 1,
        "project_human_id": project,
        "sponsor_authority": "approved",
        "iteration_human_id": "ITER-1",
        "jobs": [
            {
                "human_id": "JOB-A",
                "queue": "PM",
                "agent_role": "PM",
            }
        ],
    }


def test_projection_snapshot_isolation_pause_etag_and_no_table_leak(
    tmp_path: Path,
) -> None:
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
    accepted = client.post(
        "/v1/projects/PRJ-A/plan/accept",
        json={"plan": _plan("PRJ-A")},
    )
    assert accepted.status_code == 200, accepted.text

    initialize_database(tmp_path / "projectos.db")
    with connection(tmp_path / "projectos.db") as conn:
        delivery = create_job(
            conn,
            human_id="DEL-A",
            project_human_id="PRJ-A",
            repository_root=repo_a,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
        )
        assurance = create_job(
            conn,
            human_id="QA-A",
            project_human_id="PRJ-A",
            repository_root=repo_a,
            agent_role="ASSURANCE_FUNCTIONAL",
            queue="ASSURANCE_FUNCTIONAL",
            status="SUCCEEDED",
        )
        insert_qa_evidence(
            conn,
            project_human_id="PRJ-A",
            repository_root=repo_a,
            delivery_job_id=delivery.id,
            assurance_job_id=assurance.id,
            candidate_git_sha="abc123",
            assurance_role="ASSURANCE_FUNCTIONAL",
            result="fail",
        )
        conn.execute(
            "UPDATE qa_evidence SET defect_human_id = ? WHERE assurance_job_id = ?",
            ("BUG-1", assurance.id),
        )
        create_job(
            conn,
            human_id="JOB-B",
            project_human_id="PRJ-B",
            repository_root=repo_b,
            agent_role="PM",
            queue="PM",
            status="READY",
        )
        job_a = get_job_by_human_id(conn, "JOB-A")
        assert job_a is not None
        append_run_event(
            conn,
            job_a.id,
            "qa.failed",
            status="FAILED",
            message="recorded for projection",
        )

    first = client.get("/v1/projects/PRJ-A/projection")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["schema_version"] == 1
    assert body["project_human_id"] == "PRJ-A"
    assert body["approvals"]["has_accepted_plan"] is True
    assert body["approvals"]["sponsor_granted"] is True
    job_ids = {item["human_id"] for item in body["jobs"]["items"]}
    assert "JOB-A" in job_ids
    assert "DEL-A" in job_ids
    assert "JOB-B" not in job_ids
    assert body["defects"][0]["defect_human_id"] == "BUG-1"
    assert body["assurance"]["failed_count"] >= 1
    assert body["learning"]["usage"]["reported"] is False
    assert body["invalidations"] == []
    dumped = first.text
    assert "repository_root" not in dumped
    assert "orchestration_jobs" not in dumped
    assert "worktree_path" not in dumped
    assert first.headers.get("etag")
    cached = client.get(
        "/v1/projects/PRJ-A/projection",
        headers={"If-None-Match": first.headers["etag"]},
    )
    assert cached.status_code == 304

    initialize_database(tmp_path / "projectos.db")
    with connection(tmp_path / "projectos.db") as conn:
        set_project_paused(conn, "PRJ-A", paused=True, reason="hold")
    paused = client.get("/v1/projects/PRJ-A/projection")
    assert paused.status_code == 200
    assert paused.json()["health"]["status"] == "paused"
    assert paused.json()["health"]["paused"] is True
    other = client.get("/v1/projects/PRJ-B/projection")
    assert other.status_code == 200
    other_ids = {item["human_id"] for item in other.json()["jobs"]["items"]}
    assert other_ids == {"JOB-B"}
    assert other.json()["health"]["paused"] is False
