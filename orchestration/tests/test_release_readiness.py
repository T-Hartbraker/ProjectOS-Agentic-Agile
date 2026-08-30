"""RELEASE must not become READY until integration and QA gates pass."""

from __future__ import annotations

import json
from pathlib import Path

from projectos.db import connection
from projectos.plan import run_plan, validate_plan_document
from projectos.projectctl_bridge import ProjectctlStatusResult
from projectos.qa_handoff import REQUIRED_ASSURANCE
from projectos.release_provenance import (
    bind_dependent_release_jobs,
    promote_eligible_release_jobs,
)
from projectos.store import (
    add_job_dependency,
    create_job,
    get_job,
    get_job_by_human_id,
    list_eligible_ready_jobs,
    list_job_dependencies,
    mark_succeeded,
    promote_queued_to_ready,
)
from projectos.worker import run_once

from orch_helpers import init_git_repo, make_cursor_runner, seed_db, write_registry


def _write_identity(repo: Path, project_human_id: str = "PRJ-003") -> None:
    d = repo / "project"
    d.mkdir(parents=True, exist_ok=True)
    (d / "repository.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository_type": "delivery-project",
                "project_human_id": project_human_id,
                "project_name": "Example",
                "isolation_model": "one-project-per-repository",
                "orchestration_scope": "project",
                "cross_project_access": False,
            }
        ),
        encoding="utf-8",
    )


def _fake_status(human_id: str = "PRJ-003"):
    return lambda root: ProjectctlStatusResult(
        returncode=0,
        stdout=f"Active project: {human_id} - Example\n",
        stderr="",
        active_project_human_id=human_id,
        python_executable=Path("/fake/python"),
    )


def _cfg(tmp_path: Path, repo: Path, project_id: str = "PRJ-003") -> Path:
    _write_identity(repo, project_id)
    return write_registry(
        tmp_path / f"projects-{project_id}.json",
        [
            {
                "project_human_id": project_id,
                "repository_root": str(repo.resolve()),
                "enabled": True,
            }
        ],
    )


def _phase2_plan(*, include_release_dep: bool = False) -> dict:
    release_deps = ["JOB-P2-INTEGRATION"] if include_release_dep else []
    return {
        "schema_version": 1,
        "project_human_id": "PRJ-003",
        "iteration_human_id": "ITER-002",
        "sponsor_authority": "approved",
        "jobs": [
            {
                "human_id": "JOB-P2-PM-SETUP",
                "queue": "PM",
                "agent_role": "PM",
                "depends_on": [],
            },
            {
                "human_id": "JOB-P2-ARCH",
                "queue": "ARCHITECTURE",
                "agent_role": "ARCHITECTURE",
                "depends_on": ["JOB-P2-PM-SETUP"],
            },
            {
                "human_id": "JOB-P2-DEL-DUE",
                "queue": "DELIVERY",
                "agent_role": "DELIVERY",
                "requirement_ref": "story:US-007",
                "acceptance_criteria": ["AC-1"],
                "depends_on": ["JOB-P2-ARCH"],
            },
            {
                "human_id": "JOB-P2-INTEGRATION",
                "queue": "INTEGRATION",
                "agent_role": "INTEGRATION",
                "depends_on": ["JOB-P2-DEL-DUE"],
            },
            {
                "human_id": "JOB-P2-RELEASE",
                "queue": "RELEASE",
                "agent_role": "RELEASE",
                "depends_on": release_deps,
            },
        ],
    }


def _insert_qa(
    conn,
    delivery,
    *,
    result: str = "pass",
    include_manager: bool = True,
) -> None:
    cand = delivery.candidate_git_sha or "deadbeef"
    for role in REQUIRED_ASSURANCE:
        conn.execute(
            """
            INSERT INTO qa_evidence (
                project_human_id, repository_root, delivery_job_id,
                assurance_job_id, candidate_git_sha, assurance_role, result
            ) VALUES (?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                delivery.project_human_id,
                delivery.repository_root,
                delivery.id,
                cand,
                role,
                result,
            ),
        )
    if include_manager:
        mgr = create_job(
            conn,
            human_id=f"{delivery.human_id}__QA_MANAGER",
            project_human_id=delivery.project_human_id,
            repository_root=delivery.repository_root,
            agent_role="ASSURANCE_QUALITY",
            queue="ASSURANCE_QUALITY",
            status="READY",
            iteration_human_id=delivery.iteration_human_id,
        )
        mark_succeeded(conn, mgr.id, output_ref=None, candidate_git_sha=cand)


def test_plan_rejects_release_without_integration() -> None:
    errors = validate_plan_document(
        {
            "schema_version": 1,
            "project_human_id": "PRJ-003",
            "sponsor_authority": "approved",
            "jobs": [
                {
                    "human_id": "JOB-P2-RELEASE",
                    "queue": "RELEASE",
                    "agent_role": "RELEASE",
                    "depends_on": [],
                }
            ],
        },
        expected_project_id="PRJ-003",
    )
    assert any("INTEGRATION" in e for e in errors)


def test_freshly_planned_release_is_not_ready_before_integration(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    result = run_plan(
        project_human_id="PRJ-003",
        dry_run=False,
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        plan_override=_phase2_plan(include_release_dep=False),
    )
    assert result.status == "accepted"
    with connection(db) as conn:
        rel = get_job_by_human_id(conn, "JOB-P2-RELEASE")
        integ = get_job_by_human_id(conn, "JOB-P2-INTEGRATION")
        assert rel is not None and integ is not None
        assert rel.status == "QUEUED"
        assert rel.ready_at is None
        assert integ.status == "READY"
        assert integ.id in list_job_dependencies(conn, rel.id)
        eligible = [j.human_id for j in list_eligible_ready_jobs(conn)]
        assert "JOB-P2-RELEASE" not in eligible


def test_release_stays_non_runnable_until_integration_succeeds(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        integ = create_job(
            conn,
            human_id="JOB-P2-INTEGRATION",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="INTEGRATION",
            queue="INTEGRATION",
            status="READY",
            iteration_human_id="ITER-002",
        )
        rel = create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="QUEUED",
            iteration_human_id="ITER-002",
        )
        add_job_dependency(conn, rel.id, integ.id)
        integ_id = integ.id

    for status in ("READY", "RUNNING", "FAILED", "BLOCKED"):
        with connection(db) as conn:
            conn.execute(
                "UPDATE orchestration_jobs SET status = ? WHERE id = ?",
                (status, integ_id),
            )
        result = run_once(
            db_path=db,
            registry_path=cfg,
            job_human_id="JOB-P2-RELEASE",
            cursor_runner=make_cursor_runner(returncode=0, stdout="no"),
            skip_identity_validation=True,
            timeout_seconds=5,
        )
        assert result.status == "skipped", status
        with connection(db) as conn:
            rel = get_job_by_human_id(conn, "JOB-P2-RELEASE")
            assert rel.status == "QUEUED"
            assert rel.ready_at is None


def test_release_ready_only_after_integration_succeeded_with_candidate(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        integ = create_job(
            conn,
            human_id="JOB-P2-INTEGRATION",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="INTEGRATION",
            queue="INTEGRATION",
            status="READY",
            iteration_human_id="ITER-002",
        )
        rel = create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="QUEUED",
            iteration_human_id="ITER-002",
        )
        add_job_dependency(conn, rel.id, integ.id)
        integ = mark_succeeded(
            conn,
            integ.id,
            output_ref=None,
            candidate_git_sha="intsha001",
        )
        bound = bind_dependent_release_jobs(conn, integ)
        rel = get_job(conn, rel.id)
    assert "JOB-P2-RELEASE" in bound
    assert rel.status == "READY"
    assert rel.ready_at is not None
    assert rel.source_candidate_sha == "intsha001"


def test_missing_integration_candidate_prevents_promotion(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        integ = create_job(
            conn,
            human_id="JOB-P2-INTEGRATION",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="INTEGRATION",
            queue="INTEGRATION",
            status="READY",
            iteration_human_id="ITER-002",
        )
        rel = create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="QUEUED",
            iteration_human_id="ITER-002",
        )
        add_job_dependency(conn, rel.id, integ.id)
        integ = mark_succeeded(conn, integ.id, output_ref=None, candidate_git_sha=None)
        bind_dependent_release_jobs(conn, integ)
        rel = get_job(conn, rel.id)
        assert rel.status == "QUEUED"
        assert rel.ready_at is None
        assert promote_eligible_release_jobs(conn) == []
        rel = get_job(conn, rel.id)
        assert rel.status == "QUEUED"


def test_project_iteration_mismatch_prevents_promotion(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        integ = create_job(
            conn,
            human_id="JOB-P2-INTEGRATION",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="INTEGRATION",
            queue="INTEGRATION",
            status="READY",
            iteration_human_id="ITER-002",
        )
        rel = create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="QUEUED",
            iteration_human_id="ITER-999",
        )
        add_job_dependency(conn, rel.id, integ.id)
        integ = mark_succeeded(
            conn, integ.id, output_ref=None, candidate_git_sha="intsha002"
        )
        bind_dependent_release_jobs(conn, integ)
        rel = get_job(conn, rel.id)
        assert rel.status == "QUEUED"
        assert rel.source_candidate_sha is None


def test_qa_gate_failure_prevents_release_readiness(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        delivery = create_job(
            conn,
            human_id="JOB-P2-DEL-DUE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            iteration_human_id="ITER-002",
        )
        delivery = mark_succeeded(
            conn, delivery.id, output_ref=None, candidate_git_sha="deliv001"
        )
        _insert_qa(conn, delivery, result="fail")
        integ = create_job(
            conn,
            human_id="JOB-P2-INTEGRATION",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="INTEGRATION",
            queue="INTEGRATION",
            status="READY",
            iteration_human_id="ITER-002",
        )
        rel = create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="QUEUED",
            iteration_human_id="ITER-002",
        )
        add_job_dependency(conn, integ.id, delivery.id)
        add_job_dependency(conn, rel.id, integ.id)
        integ = mark_succeeded(
            conn, integ.id, output_ref=None, candidate_git_sha="intsha003"
        )
        bind_dependent_release_jobs(conn, integ)
        rel = get_job(conn, rel.id)
        assert rel.status == "QUEUED"


def test_passing_qa_allows_release_promotion(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        delivery = create_job(
            conn,
            human_id="JOB-P2-DEL-DUE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            iteration_human_id="ITER-002",
        )
        delivery = mark_succeeded(
            conn, delivery.id, output_ref=None, candidate_git_sha="deliv002"
        )
        _insert_qa(conn, delivery, result="pass")
        integ = create_job(
            conn,
            human_id="JOB-P2-INTEGRATION",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="INTEGRATION",
            queue="INTEGRATION",
            status="READY",
            iteration_human_id="ITER-002",
        )
        rel = create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="QUEUED",
            iteration_human_id="ITER-002",
        )
        add_job_dependency(conn, integ.id, delivery.id)
        add_job_dependency(conn, rel.id, integ.id)
        integ = mark_succeeded(
            conn, integ.id, output_ref=None, candidate_git_sha="intsha004"
        )
        bind_dependent_release_jobs(conn, integ)
        rel = get_job(conn, rel.id)
        assert rel.status == "READY"
        assert rel.source_candidate_sha == "intsha004"


def test_historical_ready_at_is_not_rewritten(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    frozen = "2026-08-21T13:47:20Z"
    with connection(db) as conn:
        integ = create_job(
            conn,
            human_id="JOB-P2-INTEGRATION",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="INTEGRATION",
            queue="INTEGRATION",
            status="READY",
            iteration_human_id="ITER-002",
        )
        rel = create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            iteration_human_id="ITER-002",
        )
        add_job_dependency(conn, rel.id, integ.id)
        conn.execute(
            "UPDATE orchestration_jobs SET ready_at = ? WHERE id = ?",
            (frozen, rel.id),
        )
        integ = mark_succeeded(
            conn, integ.id, output_ref=None, candidate_git_sha="intsha005"
        )
        bind_dependent_release_jobs(conn, integ)
        promote_queued_to_ready(conn, rel.id)
        rel = get_job(conn, rel.id)
        assert rel.ready_at == frozen
        assert rel.status == "READY"
