"""Work intake: business intent, PM plan preview, sponsor-reserved decisions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.http import create_app
from projectos.intake import assess_work_request
from projectos.migrate import initialize_database
from projectos.plan import PlanResult
from projectos.store import list_jobs_for_project

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _repo(tmp_path: Path, name: str, human_id: str) -> Path:
    repo = init_git_repo(tmp_path / name)
    write_identity(repo, project_human_id=human_id, project_name="Example")
    return repo


def _client(tmp_path: Path, human_id: str = "PRJ-A") -> TestClient:
    repo = _repo(tmp_path, "alpha", human_id)
    write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": human_id,
                "repository_root": str(repo.resolve()),
                "enabled": True,
            }
        ],
    )
    app = create_app(
        registry_path=tmp_path / "projects.json",
        db_path=tmp_path / "projectos.db",
        projectctl_runner=lambda root: fake_status(human_id),
    )
    return TestClient(app)


COMPLETE = {
    "business_request": "Operators need to find failing jobs faster on the overview.",
    "objective": "Reduce time to locate failed work during an incident.",
    "acceptance": (
        "Given failed jobs exist, when an operator opens overview they must see "
        "a failed-job count and can filter to only those jobs."
    ),
}

PLAN = {
    "schema_version": 1,
    "project_human_id": "PRJ-A",
    "sponsor_authority": "approved",
    "iteration_human_id": "ITER-1",
    "assumptions": ["PM will sequence delivery before assurance."],
    "jobs": [
        {
            "human_id": "JOB-INTAKE-A",
            "queue": "PM",
            "agent_role": "PM",
        }
    ],
}


def test_assess_surfaces_sponsor_gaps_not_pm_implementation_questions() -> None:
    assumptions, decisions = assess_work_request(
        business_request="Ship to production a new company product",
        objective="Be done",
        acceptance="do it well",
    )
    codes = {item.code for item in decisions}
    assert "untestable_acceptance" in codes
    assert "release_authorization" in codes
    assert "scope_new_venture" in codes
    assert all("queue" not in item.question.casefold() for item in decisions)
    assert any(item.owner == "pm" for item in assumptions)


def test_preview_blocks_jobs_payload_and_surfaces_decisions(tmp_path: Path) -> None:
    client = _client(tmp_path)
    rejected = client.post(
        "/v1/projects/PRJ-A/work-requests/preview",
        json={**COMPLETE, "jobs": []},
    )
    assert rejected.status_code == 422

    vague = client.post(
        "/v1/projects/PRJ-A/work-requests/preview",
        json={
            "business_request": "Make the dashboard nicer",
            "objective": "It should feel better",
            "acceptance": "looks good",
        },
    )
    assert vague.status_code == 200
    body = vague.json()
    assert body["status"] == "preview"
    assert body["expected_jobs"] == []
    assert any(item["reserved_for"] == "sponsor" for item in body["decision_requests"])
    assert any("PM owns job graph" in a["statement"] for a in body["assumptions"])

    submit = client.post(
        "/v1/projects/PRJ-A/work-requests/submit",
        json={
            "business_request": "Make the dashboard nicer",
            "objective": "It should feel better",
            "acceptance": "looks good",
        },
    )
    assert submit.status_code == 200
    assert submit.json()["status"] == "needs_sponsor_decision"
    initialize_database(tmp_path / "projectos.db")
    with connection(tmp_path / "projectos.db") as conn:
        assert list_jobs_for_project(conn, "PRJ-A") == []


def test_complete_preview_and_submit_uses_pm_plan(tmp_path: Path) -> None:
    client = _client(tmp_path)

    def fake_run_plan(**kwargs):
        dry = bool(kwargs.get("dry_run"))
        assert kwargs.get("work_request") is not None
        assert "jobs" not in (kwargs.get("work_request") or {})
        return PlanResult(
            status="dry_run" if dry else "accepted",
            project_human_id="PRJ-A",
            dry_run=dry,
            jobs_created=[] if dry else ["JOB-INTAKE-A"],
            plan=PLAN,
            plan_source="cursor",
        )

    with patch("projectos.intake.run_plan", side_effect=fake_run_plan):
        preview = client.post(
            "/v1/projects/PRJ-A/work-requests/preview",
            json=COMPLETE,
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["status"] == "preview"
        assert body["decision_requests"] == []
        assert body["expected_jobs"][0]["human_id"] == "JOB-INTAKE-A"
        assert body["expected_jobs"][0]["depends_on"] == []
        assert any("PM will sequence" in a["statement"] for a in body["assumptions"])

        submit = client.post(
            "/v1/projects/PRJ-A/work-requests/submit",
            json=COMPLETE,
        )
        assert submit.status_code == 200, submit.text
        assert submit.json()["status"] == "submitted"
        assert submit.json()["jobs_created"] == ["JOB-INTAKE-A"]
