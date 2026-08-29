"""Audit explorer is a project-scoped projection over source records."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.http import create_app
from projectos.migrate import initialize_database
from projectos.store import append_run_event, create_job

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _client(tmp_path: Path) -> TestClient:
    alpha = init_git_repo(tmp_path / "alpha")
    write_identity(alpha, project_human_id="PRJ-A", project_name="Alpha")
    write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-A",
                "repository_root": str(alpha.resolve()),
                "enabled": True,
            }
        ],
    )
    return TestClient(
        create_app(
            registry_path=tmp_path / "projects.json",
            db_path=tmp_path / "projectos.db",
            projectctl_runner=lambda root: fake_status("PRJ-A"),
            skip_identity_validation=True,
        )
    )


def test_audit_projects_source_records_with_filters(tmp_path: Path) -> None:
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    repo = tmp_path / "alpha"
    with connection(tmp_path / "projectos.db") as conn:
            job = create_job(
                conn,
                human_id="DEL-1",
                project_human_id="PRJ-A",
                repository_root=repo,
                agent_role="DELIVERY",
                queue="DELIVERY",
                status="READY",
            )
            append_run_event(
                conn,
                job.id,
                "job.created",
                status="READY",
                message="queued",
            )

    client.post(
        "/v1/projects/PRJ-A/integrations/slack/bind",
        json={"channel_id": "CAUDIT", "team_id": "T1"},
    )
    opened = client.post(
        "/v1/projects/PRJ-A/decisions",
        json={
            "action": "governance_change",
            "reason": "Needs an explicit grant",
            "impact": "Governance only",
            "requested_by": "operator",
        },
    )
    assert opened.status_code == 200, opened.text
    client.post("/v1/projects/PRJ-A/integrations/slack/notify")

    listed = client.get("/v1/projects/PRJ-A/audit")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert "projection" in body["notice"].lower() or "source" in body["notice"].lower()
    sources = {item["source"] for item in body["events"]}
    assert "orchestration" in sources
    assert "approval" in sources
    assert "slack" in sources
    assert str(tmp_path) not in str(body)

    slack_only = client.get("/v1/projects/PRJ-A/audit", params={"source": "slack"})
    assert slack_only.status_code == 200, slack_only.text
    assert slack_only.json()["events"]
    assert all(item["source"] == "slack" for item in slack_only.json()["events"])

    approval_only = client.get("/v1/projects/PRJ-A/audit", params={"actor_type": "approval"})
    assert approval_only.status_code == 200, approval_only.text
    assert approval_only.json()["events"]
    assert all(item["actor_type"] == "approval" for item in approval_only.json()["events"])
