"""Formal reports collect cited DTOs; rendering is a separate step."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.http import create_app
from projectos.learning import ingest_agent_memory, record_injections
from projectos.migrate import initialize_database
from projectos.services.report_render import (
    render_report_html,
    render_report_markdown,
    render_report_pdf,
)
from projectos.store import (
    append_run_event,
    create_job,
    insert_agent_run,
    insert_candidate_invalidation,
    insert_qa_evidence,
    set_job_outcome,
    utc_now_iso,
)

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


def _seed(tmp_path: Path) -> None:
    initialize_database(tmp_path / "projectos.db")
    repo = tmp_path / "alpha"
    now = utc_now_iso()
    with connection(tmp_path / "projectos.db") as conn:
        delivery = create_job(
            conn,
            human_id="DEL-1",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
            iteration_human_id="ITER-1",
            work_item_type="story",
            work_item_human_id="US-1",
        )
        assurance = create_job(
            conn,
            human_id="QA-FUNC",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="ASSURANCE_FUNCTIONAL",
            queue="ASSURANCE_FUNCTIONAL",
            status="SUCCEEDED",
            iteration_human_id="ITER-1",
        )
        blocked = create_job(
            conn,
            human_id="DEL-BLOCKED",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="BLOCKED",
            iteration_human_id="ITER-1",
        )
        release = create_job(
            conn,
            human_id="REL-1",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="SUCCEEDED",
            iteration_human_id="ITER-1",
        )
        set_job_outcome(conn, release.id, outcome="GATE_READY")
        conn.execute(
            "UPDATE orchestration_jobs SET last_error = ? WHERE id = ?",
            ("needs work-item context", blocked.id),
        )
        insert_qa_evidence(
            conn,
            project_human_id="PRJ-A",
            repository_root=repo,
            delivery_job_id=delivery.id,
            assurance_job_id=assurance.id,
            candidate_git_sha="abc123",
            assurance_role="ASSURANCE_FUNCTIONAL",
            result="pass",
        )
        insert_agent_run(
            conn,
            job_id=delivery.id,
            worker_id="w1",
            cursor_command=["agent"],
            prompt_ref=None,
            output_ref=None,
            stdout_ref=None,
            stderr_ref=None,
            exit_code=0,
            started_at=now,
            ended_at=now,
            duration_ms=9,
            worktree_name=None,
            worktree_path=None,
            base_git_sha=None,
            candidate_git_sha="abc123",
            dirty=False,
            usage={"input_tokens": 11, "output_tokens": 4},
            error=None,
        )
        append_run_event(
            conn,
            delivery.id,
            "learning.recorded",
            status="SUCCEEDED",
            message="pattern stored",
        )


def test_reports_catalog_and_cited_dtos_are_reproducible(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _seed(tmp_path)

    catalog = client.get("/v1/projects/PRJ-A/reports")
    assert catalog.status_code == 200, catalog.text
    kinds = {item["kind"] for item in catalog.json()["reports"]}
    assert kinds == {
        "project-status",
        "iteration-review",
        "quality",
        "release",
        "risks",
        "usage",
        "learning",
    }

    status = client.get("/v1/projects/PRJ-A/reports/project-status")
    assert status.status_code == 200, status.text
    payload = status.json()
    assert payload["report_kind"] == "project-status"
    assert payload["body"]["health"] in {"healthy", "blocked", "degraded"}
    source_ids = {src["entity_human_id"] for src in payload["sources"]}
    assert "DEL-1" in source_ids
    assert "QA-FUNC" in source_ids
    assert payload["revision"]
    assert payload["generated_at"]
    assert "repository_root" not in status.text

    again = client.get("/v1/projects/PRJ-A/reports/project-status")
    assert again.json()["revision"] == payload["revision"]

    quality = client.get("/v1/projects/PRJ-A/reports/quality")
    assert quality.status_code == 200, quality.text
    assert quality.json()["body"]["summary"]["role_results"]["ASSURANCE_FUNCTIONAL"] == "pass"

    usage = client.get("/v1/projects/PRJ-A/reports/usage")
    assert usage.status_code == 200, usage.text
    assert usage.json()["body"]["reported"] is True
    assert usage.json()["body"]["input_tokens"] == 11
    assert usage.json()["body"]["output_tokens"] == 4

    risks = client.get("/v1/projects/PRJ-A/reports/risks")
    assert risks.status_code == 200, risks.text
    risk_jobs = {item["job_human_id"] for item in risks.json()["body"]["issues"]}
    assert "DEL-BLOCKED" in risk_jobs

    learning = client.get("/v1/projects/PRJ-A/reports/learning")
    assert learning.status_code == 200, learning.text
    assert learning.json()["body"]["job_success_count"] >= 1
    assert learning.json()["body"]["learning_event_count"] >= 1
    assert learning.json()["body"]["assurance_pass_count"] == 1

    release = client.get("/v1/projects/PRJ-A/reports/release")
    assert release.status_code == 200, release.text
    assert release.json()["body"]["latest"]["release_human_id"] == "REL-1"

    review = client.get(
        "/v1/projects/PRJ-A/reports/iteration-review",
        params={"iteration_human_id": "ITER-1"},
    )
    assert review.status_code == 200, review.text
    assert review.json()["iteration_human_id"] == "ITER-1"
    work_items = {item["work_item_human_id"] for item in review.json()["body"]["work_items"]}
    assert "US-1" in work_items

    missing = client.get("/v1/projects/PRJ-A/reports/unknown-kind")
    assert missing.status_code == 404
    traversal = client.get("/v1/projects/PRJ-A/reports/..%2Fsecret")
    assert traversal.status_code == 404

    markdown = render_report_markdown(payload)
    assert "Project Status" in markdown
    assert "DEL-1" in markdown
    assert payload["revision"] in markdown
    assert "C:\\" not in markdown


def test_report_render_uses_only_the_dto() -> None:
    report = {
        "title": "Project Status",
        "report_kind": "project-status",
        "project_human_id": "PRJ-A",
        "iteration_human_id": "ITER-1",
        "generated_at": "2026-01-01T00:00:00Z",
        "revision": "abc123",
        "sources": [
            {
                "entity_type": "job",
                "entity_human_id": "DEL-1",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        ],
        "body": {"health": "healthy"},
    }
    markdown = render_report_markdown(report)
    assert "job DEL-1" in markdown
    assert "healthy" in markdown
    assert "generated snapshot" in markdown
    html = render_report_html(report)
    assert "PRJ-A" in html
    assert "ITER-1" in html
    assert "abc123" in html
    assert "generated snapshot" in html
    pdf = render_report_pdf(report)
    assert pdf.startswith(b"%PDF-")
    assert b"PRJ-A" in pdf


def test_report_downloads_are_snapshots_and_do_not_mutate_state(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _seed(tmp_path)
    first = client.get("/v1/projects/PRJ-A/reports/project-status")
    assert first.status_code == 200, first.text
    revision = first.json()["revision"]

    html = client.get("/v1/projects/PRJ-A/reports/project-status/download", params={"format": "html"})
    assert html.status_code == 200, html.text
    assert "text/html" in html.headers.get("content-type", "")
    disposition = html.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "filename=" in disposition
    filename = disposition.split("filename=")[-1].strip('"')
    assert "/" not in filename
    assert "\\" not in filename
    assert ".." not in filename
    body = html.content.decode("utf-8")
    assert "PRJ-A" in body
    assert "ITER-1" in body
    assert revision in body
    assert "Generated at" in body
    assert "snapshot" in body.lower()
    assert "not the system of record" in body.lower()

    markdown = client.get(
        "/v1/projects/PRJ-A/reports/project-status/download",
        params={"format": "markdown"},
    )
    assert markdown.status_code == 200
    assert first.json()["revision"] in markdown.text

    pdf = client.get("/v1/projects/PRJ-A/reports/project-status/download", params={"format": "pdf"})
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF-")
    assert "application/pdf" in pdf.headers.get("content-type", "")

    after = client.get("/v1/projects/PRJ-A/reports/project-status")
    assert after.json()["revision"] == revision

    unknown = client.get(
        "/v1/projects/PRJ-A/reports/project-status/download",
        params={"format": "exe"},
    )
    assert unknown.status_code == 404
    traversal = client.get(
        "/v1/projects/PRJ-A/reports/project-status/download",
        params={"format": "../html"},
    )
    assert traversal.status_code == 404


def test_reports_dashboard_is_live_and_snapshots_are_historical(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _seed(tmp_path)

    board = client.get("/v1/projects/PRJ-A/reports/dashboard")
    assert board.status_code == 200, board.text
    payload = board.json()
    assert payload["origin"] == "live"
    assert "system of record" in payload["notice"].lower() or "not the system of record" in payload["notice"].lower()
    kinds = [item["report_kind"] for item in payload["reports"]]
    assert kinds == [
        "project-status",
        "iteration-review",
        "quality",
        "release",
        "risks",
        "usage",
        "learning",
    ]
    assert all(item["origin"] == "live" for item in payload["reports"])
    assert payload["snapshots"] == []

    saved = client.post("/v1/projects/PRJ-A/reports/project-status/snapshots", json={})
    assert saved.status_code == 200, saved.text
    snap = saved.json()
    assert snap["origin"] == "snapshot"
    assert snap["snapshot_human_id"].startswith("RPT-")
    assert snap["revision"] == payload["reports"][0]["revision"]

    after_live = client.get("/v1/projects/PRJ-A/reports/dashboard")
    assert after_live.json()["origin"] == "live"
    assert after_live.json()["reports"][0]["revision"] == payload["reports"][0]["revision"]
    assert len(after_live.json()["snapshots"]) == 1

    viewed = client.get(
        f"/v1/projects/PRJ-A/reports/snapshots/{snap['snapshot_human_id']}"
    )
    assert viewed.status_code == 200, viewed.text
    assert viewed.json()["origin"] == "snapshot"
    assert viewed.json()["snapshot_human_id"] == snap["snapshot_human_id"]
    assert viewed.json()["revision"] == snap["revision"]

    html = client.get(
        f"/v1/projects/PRJ-A/reports/snapshots/{snap['snapshot_human_id']}/download",
        params={"format": "html"},
    )
    assert html.status_code == 200
    assert snap["revision"] in html.content.decode("utf-8")
    assert "not the system of record" in html.content.decode("utf-8").lower()

    jobs = client.get("/v1/projects/PRJ-A/jobs")
    assert jobs.status_code == 200
    assert {job["human_id"] for job in jobs.json()["jobs"]} >= {"DEL-1", "QA-FUNC"}



def test_learning_report_joins_lineage_without_claiming_cause(tmp_path: Path) -> None:
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    repo = tmp_path / "alpha"
    with connection(tmp_path / "projectos.db") as conn:
        source = create_job(
            conn,
            human_id="DEL-MEM-1",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
            iteration_human_id="ITER-1",
            work_item_type="story",
            work_item_human_id="US-MEM",
        )
        used = ingest_agent_memory(
            conn,
            project_human_id="PRJ-A",
            agent_role="DELIVERY",
            title="Retry git locks with a bounded backoff",
            evidence_ref="notes.md",
            source_job_human_id=source.human_id,
        )
        ingest_agent_memory(
            conn,
            project_human_id="PRJ-A",
            agent_role="DELIVERY",
            title="Retry git locks with a bounded backoff",
            source_job_human_id=source.human_id,
        )
        unused = ingest_agent_memory(
            conn,
            project_human_id="PRJ-A",
            agent_role="DELIVERY",
            title="Prefer rebase over merge for delivery branches",
            evidence_ref="unused.md",
            source_job_human_id=source.human_id,
        )
        record_injections(
            conn,
            project_human_id="PRJ-A",
            job_human_id=source.human_id,
            agent_run_id=None,
            memories=[used],
        )
        later = create_job(
            conn,
            human_id="DEL-MEM-2",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="FAILED",
            iteration_human_id="ITER-1",
            work_item_type="story",
            work_item_human_id="US-MEM",
        )
        qa = create_job(
            conn,
            human_id="QA-MEM-2",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="ASSURANCE_FUNCTIONAL",
            queue="ASSURANCE_FUNCTIONAL",
            status="SUCCEEDED",
            iteration_human_id="ITER-1",
            work_item_human_id="US-MEM",
        )
        insert_qa_evidence(
            conn,
            project_human_id="PRJ-A",
            repository_root=repo,
            delivery_job_id=later.id,
            assurance_job_id=qa.id,
            candidate_git_sha="def456",
            assurance_role="ASSURANCE_FUNCTIONAL",
            result="fail",
        )
        conn.execute(
            "UPDATE qa_evidence SET defect_human_id = ? WHERE assurance_job_id = ?",
            ("BUG-MEM", qa.id),
        )
        rework = create_job(
            conn,
            human_id="DEL-MEM-2__REWORK",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            iteration_human_id="ITER-1",
            work_item_human_id="US-MEM",
        )
        insert_candidate_invalidation(
            conn,
            delivery_job_id=later.id,
            invalidated_candidate_sha="def456",
            reason="same lock failure recurred",
            rework_job_id=rework.id,
        )

    body = client.get("/v1/projects/PRJ-A/reports/learning")
    assert body.status_code == 200, body.text
    payload = body.json()["body"]
    assert "correlation" in payload["caveat"].lower()
    assert "not proof" in payload["caveat"].lower()
    assert "causal" in payload["note"].lower()
    used_id = used["memory_human_id"]
    unused_id = unused["memory_human_id"]
    unused_ids = {item["memory_human_id"] for item in payload["unused_memories"]}
    repeated_ids = {item["memory_human_id"] for item in payload["repeated_failure_after_memory"]}
    reinforced_ids = {item["memory_human_id"] for item in payload["reinforced_lessons"]}
    assert unused_id in unused_ids
    assert used_id not in unused_ids
    assert used_id in repeated_ids
    assert used_id in reinforced_ids
    used_row = next(item for item in payload["memories"] if item["memory_human_id"] == used_id)
    assert used_row["injection_count"] >= 1
    assert used_row["reinforcement_count"] >= 1
    assert used_row["source_job_human_id"] == "DEL-MEM-1"
    assert used_row["evidence_ref"] == "notes.md"
    assert "DEL-MEM-2" in used_row["subsequent_related_failure_job_human_ids"]
    assert "DEL-MEM-2__REWORK" in used_row["subsequent_rework_job_human_ids"]
    assert used_row["recurrence_observed"] is True
    source_ids = {item["entity_human_id"] for item in body.json()["sources"]}
    assert used_id in source_ids
    assert "caused" not in payload["caveat"].lower() or "not proof" in payload["caveat"].lower()
