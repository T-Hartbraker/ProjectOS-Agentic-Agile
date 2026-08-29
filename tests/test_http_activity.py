"""Job graph and activity expose safe execution DTOs, not filesystem browsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.http import create_app
from projectos.migrate import initialize_database
from projectos.store import create_job, insert_agent_run, public_artifact_ref, utc_now_iso

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _client(tmp_path: Path) -> TestClient:
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-A", project_name="Example")
    write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-A",
                "repository_root": str(repo.resolve()),
                "enabled": True,
            }
        ],
    )
    app = create_app(
        registry_path=tmp_path / "projects.json",
        db_path=tmp_path / "projectos.db",
        projectctl_runner=lambda root: fake_status("PRJ-A"),
    )
    return TestClient(app)


def test_public_artifact_ref_strips_paths() -> None:
    assert public_artifact_ref(r"C:\runs\abc\stdout.txt") == "stdout.txt"
    assert public_artifact_ref("run-1/output.json") == "output.json"
    assert public_artifact_ref(None) is None


def test_jobs_graph_and_activity_are_safe_dtos(tmp_path: Path) -> None:
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    with connection(tmp_path / "projectos.db") as conn:
        delivery = create_job(
            conn,
            human_id="DEL-1",
            project_human_id="PRJ-A",
            repository_root=tmp_path / "alpha",
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="RUNNING",
            work_item_type="story",
            work_item_human_id="US-1",
        )
        assurance = create_job(
            conn,
            human_id="QA-1",
            project_human_id="PRJ-A",
            repository_root=tmp_path / "alpha",
            agent_role="ASSURANCE_FUNCTIONAL",
            queue="ASSURANCE_FUNCTIONAL",
            status="READY",
        )
        from projectos.store import add_job_dependency

        add_job_dependency(conn, assurance.id, delivery.id)
        insert_agent_run(
            conn,
            job_id=delivery.id,
            worker_id="w1",
            cursor_command=["agent"],
            prompt_ref=r"C:\secret\prompts\prompt.txt",
            output_ref=r"C:\secret\runs\run-1\output.json",
            stdout_ref=r"C:\secret\runs\run-1\stdout.txt",
            stderr_ref=None,
            exit_code=0,
            started_at=utc_now_iso(),
            ended_at=utc_now_iso(),
            duration_ms=12,
            worktree_name=None,
            worktree_path=None,
            base_git_sha=None,
            candidate_git_sha="abc123def456",
            dirty=False,
            usage=None,
            error=None,
        )

    jobs = client.get("/v1/projects/PRJ-A/jobs")
    assert jobs.status_code == 200
    by_id = {job["human_id"]: job for job in jobs.json()["jobs"]}
    assert by_id["DEL-1"]["lane"] == "delivery"
    assert by_id["QA-1"]["lane"] == "assurance"
    assert by_id["QA-1"]["depends_on"] == ["DEL-1"]
    assert by_id["DEL-1"]["work_item_human_id"] == "US-1"
    assert by_id["DEL-1"]["attempt"] == 0
    assert "repository_root" not in by_id["DEL-1"]
    assert "worktree_path" not in by_id["DEL-1"]

    graph = client.get("/v1/projects/PRJ-A/graph")
    assert {"job_human_id": "QA-1", "depends_on": "DEL-1"} in graph.json()["edges"]

    activity = client.get("/v1/projects/PRJ-A/activity")
    assert activity.status_code == 200
    body = activity.json()
    assert body["in_flight"][0]["human_id"] == "DEL-1"
    run = body["recent_runs"][0]
    assert run["evidence_ref"] == "output.json"
    assert run["prompt_ref"] == "prompt.txt"
    assert run["candidate_git_sha"] == "abc123def456"
    dumped = activity.text
    assert "C:\\secret" not in dumped
    assert "repository_root" not in dumped
