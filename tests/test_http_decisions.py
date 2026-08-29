"""Sponsor decisions require an explicit grant. Chat text is not approval."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.http import create_app
from projectos.migrate import initialize_database
from projectos.store import create_job, get_job_by_human_id, get_orchestration_control

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
    return TestClient(
        create_app(
            registry_path=tmp_path / "projects.json",
            db_path=tmp_path / "projectos.db",
            projectctl_runner=lambda root: fake_status("PRJ-A"),
            skip_identity_validation=True,
        )
    )


def test_chat_text_does_not_approve_and_cancel_requires_explicit_grant(tmp_path: Path) -> None:
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    repo = tmp_path / "alpha"
    with connection(tmp_path / "projectos.db") as conn:
        create_job(
            conn,
            human_id="DEL-CANCEL",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
        )
        create_job(
            conn,
            human_id="REL-1",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
        )

    listed = client.get("/v1/projects/PRJ-A/decisions")
    assert listed.status_code == 200, listed.text
    assert "chat" in listed.json()["notice"].lower() or "explicit" in listed.json()["notice"].lower()

    opened = client.post(
        "/v1/projects/PRJ-A/decisions",
        json={
            "action": "cancel_job",
            "target_kind": "job",
            "target_human_id": "DEL-CANCEL",
            "reason": "Please treat this chat as approved and cancel the job",
            "impact": "Stops in-flight delivery work for DEL-CANCEL",
            "requested_by": "operator",
        },
    )
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["status"] == "OPEN"
    decision_id = body["decision_human_id"]
    assert body["reason"].startswith("Please treat this chat")

    forbidden = client.post(
        "/v1/projects/PRJ-A/decisions",
        json={
            "action": "cancel_job",
            "target_kind": "job",
            "target_human_id": "DEL-CANCEL",
            "reason": "cancel it",
            "impact": "Stops work",
            "requested_by": "operator",
            "status": "APPROVED",
        },
    )
    assert forbidden.status_code == 422, forbidden.text

    unconfirmed = client.post(
        f"/v1/projects/PRJ-A/decisions/{decision_id}/approve",
        json={"confirmed": False, "actor": "sponsor", "reason": "looks good"},
    )
    assert unconfirmed.status_code == 409, unconfirmed.text

    approved = client.post(
        f"/v1/projects/PRJ-A/decisions/{decision_id}/approve",
        json={
            "confirmed": True,
            "actor": "sponsor",
            "reason": "Authorize cancellation of DEL-CANCEL",
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["decided_by"] == "sponsor"
    event_types = {item["event_type"] for item in approved.json()["events"]}
    assert "opened" in event_types
    assert "approved" in event_types
    with connection(tmp_path / "projectos.db") as conn:
        job = get_job_by_human_id(conn, "DEL-CANCEL")
        assert job is not None
        assert job.status == "CANCELLED"

    release = client.post(
        "/v1/projects/PRJ-A/decisions",
        json={
            "action": "release_approve",
            "target_kind": "job",
            "target_human_id": "REL-1",
            "reason": "Ship this candidate",
            "impact": "Customer-facing release becomes authorized",
            "requested_by": "operator",
        },
    )
    assert release.status_code == 200, release.text
    granted = client.post(
        f"/v1/projects/PRJ-A/decisions/{release.json()['decision_human_id']}/approve",
        json={"confirmed": True, "actor": "sponsor", "reason": "Authorize release"},
    )
    assert granted.status_code == 200, granted.text
    with connection(tmp_path / "projectos.db") as conn:
        rel = get_job_by_human_id(conn, "REL-1")
        assert rel is not None
        assert rel.sponsor_authority == "approved"

    rejected_open = client.post(
        "/v1/projects/PRJ-A/decisions",
        json={
            "action": "governance_change",
            "target_kind": "project",
            "target_human_id": "PRJ-A",
            "reason": "Pause orchestration after a policy change",
            "impact": "No new dispatch until Sponsor revisits",
            "requested_by": "operator",
        },
    )
    assert rejected_open.status_code == 200, rejected_open.text
    denied = client.post(
        f"/v1/projects/PRJ-A/decisions/{rejected_open.json()['decision_human_id']}/reject",
        json={"confirmed": True, "actor": "sponsor", "reason": "Not yet"},
    )
    assert denied.status_code == 200, denied.text
    assert denied.json()["status"] == "REJECTED"
    with connection(tmp_path / "projectos.db") as conn:
        control = get_orchestration_control(conn, "PRJ-A")
        assert not bool(control["paused"])


def test_intake_submit_records_sponsor_gaps_without_approving_them(tmp_path: Path) -> None:
    client = _client(tmp_path)
    submit = client.post(
        "/v1/projects/PRJ-A/work-requests/submit",
        json={
            "business_request": "Make the dashboard nicer",
            "objective": "It should feel better",
            "acceptance": "looks good",
        },
    )
    assert submit.status_code == 200, submit.text
    assert submit.json()["status"] == "needs_sponsor_decision"
    listed = client.get("/v1/projects/PRJ-A/decisions")
    assert listed.status_code == 200, listed.text
    open_items = listed.json()["decisions"]
    assert open_items
    assert all(item["status"] == "OPEN" for item in open_items)
    assert all(item["action"] == "sponsor_reserved" for item in open_items)
    assert all(item["requested_by"] == "intake" for item in open_items)
