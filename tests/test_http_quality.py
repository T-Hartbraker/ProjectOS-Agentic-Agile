"""Quality/defect views expose independent assurance; developers cannot mark QA passed."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.errors import OrchestrationError
from projectos.http import create_app
from projectos.migrate import initialize_database
from projectos.qa_handoff import record_assurance_result
from projectos.store import (
    add_job_dependency,
    create_job,
    insert_candidate_invalidation,
    insert_qa_evidence,
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


def test_quality_view_is_read_only_and_strips_evidence_paths(tmp_path: Path) -> None:
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    repo = tmp_path / "alpha"
    with connection(tmp_path / "projectos.db") as conn:
        delivery = create_job(
            conn,
            human_id="DEL-1",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
            work_item_type="story",
            work_item_human_id="US-1",
        )
        conn.execute(
            "UPDATE orchestration_jobs SET candidate_git_sha = ? WHERE id = ?",
            ("abc123def456", delivery.id),
        )
        functional = create_job(
            conn,
            human_id="QA-FUNC",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="ASSURANCE_FUNCTIONAL",
            queue="ASSURANCE_FUNCTIONAL",
            status="SUCCEEDED",
        )
        security = create_job(
            conn,
            human_id="QA-SEC",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="ASSURANCE_SECURITY",
            queue="ASSURANCE_SECURITY",
            status="FAILED",
        )
        quality_job = create_job(
            conn,
            human_id="QA-QUAL",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="ASSURANCE_QUALITY",
            queue="ASSURANCE_QUALITY",
            status="READY",
        )
        rework = create_job(
            conn,
            human_id="QA-SEC__REWORK",
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
            status="BLOCKED",
        )
        add_job_dependency(conn, functional.id, delivery.id)
        add_job_dependency(conn, security.id, delivery.id)
        add_job_dependency(conn, quality_job.id, delivery.id)
        add_job_dependency(conn, rework.id, security.id)
        func_ev = insert_qa_evidence(
            conn,
            project_human_id="PRJ-A",
            repository_root=repo,
            delivery_job_id=delivery.id,
            assurance_job_id=functional.id,
            candidate_git_sha="abc123def456",
            assurance_role="ASSURANCE_FUNCTIONAL",
            result="pass",
        )
        sec_ev = insert_qa_evidence(
            conn,
            project_human_id="PRJ-A",
            repository_root=repo,
            delivery_job_id=delivery.id,
            assurance_job_id=security.id,
            candidate_git_sha="abc123def456",
            assurance_role="ASSURANCE_SECURITY",
            result="fail",
        )
        insert_qa_evidence(
            conn,
            project_human_id="PRJ-A",
            repository_root=repo,
            delivery_job_id=delivery.id,
            assurance_job_id=quality_job.id,
            candidate_git_sha="abc123def456",
            assurance_role="ASSURANCE_QUALITY",
            result="pending",
        )
        conn.execute(
            "UPDATE qa_evidence SET evidence_ref = ? WHERE id = ?",
            (r"C:\secret\runs\func\report.json", func_ev),
        )
        conn.execute(
            """
            UPDATE qa_evidence
            SET evidence_ref = ?, defect_human_id = ?
            WHERE id = ?
            """,
            (r"C:\secret\runs\sec\scan.json", "BUG-9", sec_ev),
        )
        insert_candidate_invalidation(
            conn,
            delivery_job_id=delivery.id,
            invalidated_candidate_sha="oldsha000111",
            reason="candidate superseded after QA fail",
            rework_job_id=rework.id,
        )

    body = client.get("/v1/projects/PRJ-A/quality")
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["project_human_id"] == "PRJ-A"
    assert payload["developer_can_mark_qa_passed"] is False
    assert payload["qa_pass_authority"] == "independent_assurance_only"
    assert payload["summary"]["passed_count"] == 1
    assert payload["summary"]["failed_count"] == 1
    assert payload["summary"]["pending_count"] == 1
    assert "abc123def456" in payload["summary"]["evaluated_candidate_shas"]
    assert payload["summary"]["role_results"]["ASSURANCE_FUNCTIONAL"] == "pass"
    assert payload["summary"]["role_results"]["ASSURANCE_SECURITY"] == "fail"
    assert payload["summary"]["role_results"]["ASSURANCE_INTEGRATION"] == "missing"
    evidence_refs = {item["evidence_ref"] for item in payload["evidence"] if item["evidence_ref"]}
    assert evidence_refs == {"report.json", "scan.json"}
    assert payload["findings"]["security"]["result"] == "fail"
    assert payload["findings"]["security"]["evidence_ref"] == "scan.json"
    assert payload["defects"][0]["defect_human_id"] == "BUG-9"
    assert payload["defects"][0]["status"] == "open"
    assert payload["defect_counts"]["by_status"]["open"] == 1
    kinds = {item["kind"] for item in payload["lineage"]}
    assert "invalidation" in kinds
    assert "rework" in kinds
    assert any("ASSURANCE_SECURITY" in reason for reason in payload["release_blocking_reasons"])
    assert any("open defect" in reason for reason in payload["release_blocking_reasons"])
    dumped = body.text
    assert "C:\\secret" not in dumped
    assert "repository_root" not in dumped
    assert "worktree_path" not in dumped

    denied = client.post("/v1/projects/PRJ-A/quality", json={"result": "pass"})
    assert denied.status_code == 405
    pass_denied = client.post("/v1/projects/PRJ-A/quality/pass", json={})
    assert pass_denied.status_code in {404, 405}


def test_developer_job_cannot_record_qa_pass(tmp_path: Path) -> None:
    _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    with connection(tmp_path / "projectos.db") as conn:
        delivery = create_job(
            conn,
            human_id="DEL-DEV",
            project_human_id="PRJ-A",
            repository_root=tmp_path / "alpha",
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
        )
        with pytest.raises(OrchestrationError, match="independent assurance"):
            record_assurance_result(conn, delivery, passed=True, evidence_ref="cheat.json")
