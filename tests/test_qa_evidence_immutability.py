"""QA evidence immutability tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.qa_evidence_policy import QAEvidenceImmutableError, update_qa_evidence_result
from projectos.store import create_job, insert_qa_evidence


def _db(tmp_path: Path):
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return db, str(repo.resolve())


def _seed_pending_evidence(conn, repo_root: str, *, result_after: str = "fail") -> tuple[int, int]:
    delivery = create_job(
        conn,
        human_id="DEL-1",
        project_human_id="PRJ-003",
        repository_root=repo_root,
        agent_role="DELIVERY",
        queue="DELIVERY",
        status="SUCCEEDED",
        base_git_sha="shaA",
    )
    assurance = create_job(
        conn,
        human_id="QA-1",
        project_human_id="PRJ-003",
        repository_root=repo_root,
        agent_role="ASSURANCE_FUNCTIONAL",
        queue="ASSURANCE_FUNCTIONAL",
        status="SUCCEEDED",
        base_git_sha="shaA",
    )
    evidence_id = insert_qa_evidence(
        conn,
        project_human_id="PRJ-003",
        repository_root=repo_root,
        delivery_job_id=delivery.id,
        assurance_job_id=assurance.id,
        candidate_git_sha="shaA",
        assurance_role="ASSURANCE_FUNCTIONAL",
        result="pending",
    )
    update_qa_evidence_result(
        conn,
        assurance_job_id=assurance.id,
        candidate_git_sha="shaA",
        new_result=result_after,
    )
    return evidence_id, assurance.id


def test_failed_qa_evidence_remains_failed_after_remediation_attempt(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        evidence_id, _ = _seed_pending_evidence(conn, repo_root, result_after="fail")
        row = conn.execute("SELECT result FROM qa_evidence WHERE id = ?", (evidence_id,)).fetchone()
    assert row["result"] == "fail"


def test_cannot_mutate_fail_to_pass(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        _, assurance_id = _seed_pending_evidence(conn, repo_root, result_after="fail")
        with pytest.raises(QAEvidenceImmutableError):
            update_qa_evidence_result(
                conn,
                assurance_job_id=assurance_id,
                candidate_git_sha="shaA",
                new_result="pass",
            )


def test_retest_creates_new_evidence_rows(tmp_path: Path) -> None:
    db, repo_root = _db(tmp_path)
    with connection(db) as conn:
        _seed_pending_evidence(conn, repo_root, result_after="fail")
        delivery = create_job(
            conn,
            human_id="DEL-2",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
        )
        assurance = create_job(
            conn,
            human_id="QA-2",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="ASSURANCE_FUNCTIONAL",
            queue="ASSURANCE_FUNCTIONAL",
            status="SUCCEEDED",
            base_git_sha="shaB",
        )
        insert_qa_evidence(
            conn,
            project_human_id="PRJ-003",
            repository_root=repo_root,
            delivery_job_id=delivery.id,
            assurance_job_id=assurance.id,
            candidate_git_sha="shaB",
            assurance_role="ASSURANCE_FUNCTIONAL",
            result="pending",
        )
        update_qa_evidence_result(
            conn,
            assurance_job_id=assurance.id,
            candidate_git_sha="shaB",
            new_result="pass",
        )
        rows = conn.execute(
            "SELECT candidate_git_sha, result FROM qa_evidence ORDER BY id ASC"
        ).fetchall()
    assert rows[0]["candidate_git_sha"] == "shaA"
    assert rows[0]["result"] == "fail"
    assert rows[1]["candidate_git_sha"] == "shaB"
    assert rows[1]["result"] == "pass"
