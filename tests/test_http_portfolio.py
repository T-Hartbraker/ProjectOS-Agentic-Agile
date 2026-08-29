"""Portfolio is the only normal cross-project view and does not merge stores."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.http import create_app
from projectos.migrate import initialize_database
from projectos.store import create_job

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def test_portfolio_summarizes_projects_without_merging_records(tmp_path: Path) -> None:
    alpha = init_git_repo(tmp_path / "alpha")
    bravo = init_git_repo(tmp_path / "bravo")
    write_identity(alpha, project_human_id="PRJ-A", project_name="Alpha")
    write_identity(bravo, project_human_id="PRJ-B", project_name="Bravo")
    write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-A",
                "repository_root": str(alpha.resolve()),
                "enabled": True,
            },
            {
                "project_human_id": "PRJ-B",
                "repository_root": str(bravo.resolve()),
                "enabled": True,
            },
        ],
    )
    client = TestClient(
        create_app(
            registry_path=tmp_path / "projects.json",
            db_path=tmp_path / "projectos.db",
            projectctl_runner=lambda root: fake_status(
                "PRJ-A" if "alpha" in str(root) else "PRJ-B"
            ),
            skip_identity_validation=True,
        )
    )
    initialize_database(tmp_path / "projectos.db")
    with connection(tmp_path / "projectos.db") as conn:
        create_job(
            conn,
            human_id="DEL-A",
            project_human_id="PRJ-A",
            repository_root=alpha,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            iteration_human_id="ITER-A",
        )
        create_job(
            conn,
            human_id="DEL-B",
            project_human_id="PRJ-B",
            repository_root=bravo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="BLOCKED",
            iteration_human_id="ITER-B",
        )

    listed = client.get("/v1/portfolio")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert "cross-project" in body["notice"].lower() or "own records" in body["notice"].lower()
    by_id = {item["project_human_id"]: item for item in body["projects"]}
    assert set(by_id) == {"PRJ-A", "PRJ-B"}
    assert by_id["PRJ-A"]["active_job_count"] >= 1
    assert by_id["PRJ-B"]["blocker_count"] >= 1
    assert "repository_root" not in by_id["PRJ-A"]
    assert str(tmp_path) not in str(body)
    assert by_id["PRJ-A"]["current_iteration_human_id"] == "ITER-A"
    assert by_id["PRJ-B"]["current_iteration_human_id"] == "ITER-B"
