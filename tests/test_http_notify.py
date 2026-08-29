"""Idempotent Slack notices cover important delivery events only."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.http import create_app
from projectos.migrate import initialize_database
from projectos.store import create_job, mark_failure, mark_succeeded, set_job_outcome

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _client(tmp_path: Path) -> TestClient:
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
    return TestClient(
        create_app(
            registry_path=tmp_path / "projects.json",
            db_path=tmp_path / "projectos.db",
            projectctl_runner=lambda root: fake_status(
                "PRJ-A" if "alpha" in str(root) else "PRJ-B"
            ),
            skip_identity_validation=True,
        )
    )


def test_notify_posts_idempotent_delivery_events_without_worker_flood(tmp_path: Path) -> None:
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
        done = create_job(
            conn,
            human_id="DEL-DONE",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            iteration_human_id="ITER-1",
        )
        mark_succeeded(conn, done.id, output_ref=None, candidate_git_sha=None)
        release = create_job(
            conn,
            human_id="REL-1",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
        )
        set_job_outcome(conn, release.id, outcome="GATE_READY")
        failed = create_job(
            conn,
            human_id="REL-FAIL",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            max_attempts=1,
        )
        mark_failure(conn, failed.id, error="recovery failed: salvage aborted")
        create_job(
            conn,
            human_id="DEL-NOISE",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="FAILED",
        )

    unbound = client.post("/v1/projects/PRJ-A/integrations/slack/notify")
    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["posted"] == []

    bound = client.post(
        "/v1/projects/PRJ-A/integrations/slack/bind",
        json={"channel_id": "CNOTIFY", "team_id": "T1"},
    )
    assert bound.status_code == 200, bound.text

    opened = client.post(
        "/v1/projects/PRJ-A/decisions",
        json={
            "action": "cancel_job",
            "reason": "Sponsor must confirm cancellation",
            "impact": "Stops DEL-CANCEL",
            "requested_by": "operator",
            "target_kind": "job",
            "target_human_id": "DEL-CANCEL",
        },
    )
    assert opened.status_code == 200, opened.text
    decision_id = opened.json()["decision_human_id"]

    first = client.post("/v1/projects/PRJ-A/integrations/slack/notify")
    assert first.status_code == 200, first.text
    posted = first.json()["posted"]
    kinds = {item["kind"] for item in posted}
    assert "sponsor_decision_required" in kinds
    assert "iteration_review_ready" in kinds
    assert "release_ready" in kinds
    assert "recovery_failure" in kinds
    assert "released" not in kinds
    texts = " ".join(item["text"] for item in posted)
    assert "PRJ-A" in texts
    assert "/projects/PRJ-A" in texts
    assert str(tmp_path) not in texts
    assert "\\" not in texts
    assert all(item["entity_human_id"] != "DEL-NOISE" for item in posted)

    again = client.post("/v1/projects/PRJ-A/integrations/slack/notify")
    assert again.status_code == 200, again.text
    assert again.json()["posted"] == []
    assert decision_id in again.json()["already_posted"]

    listed = client.get("/v1/projects/PRJ-A/integrations/slack/notifications")
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["notifications"]) == len(posted)
    assert listed.json()["project_human_id"] == "PRJ-A"
