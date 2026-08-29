"""RELEASE must evaluate and persist the integrated candidate, not repo HEAD."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from projectos.db import connection
from projectos.prompt_builder import build_role_prompt
from projectos.release_provenance import bind_dependent_release_jobs
from projectos.store import (
    add_job_dependency,
    create_job,
    get_job,
    get_job_by_human_id,
    mark_succeeded,
    set_job_source_provenance,
)
from projectos.release_readiness import GATE_READY_OUTCOME, ReleaseEvaluation
from projectos.worker import run_once
from projectos.worktree import current_head_sha

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


def _commit_then_reset(repo: Path, filename: str, message: str) -> tuple[str, str]:
    base = current_head_sha(repo)
    (repo / filename).write_text(message, encoding="utf-8")
    subprocess.run(
        ["git", "add", filename], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True
    )
    candidate = current_head_sha(repo)
    subprocess.run(
        ["git", "reset", "--hard", base], cwd=repo, check=True, capture_output=True
    )
    return base, candidate


def test_release_prompt_includes_integrated_candidate(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
        )
        set_job_source_provenance(
            conn,
            job.id,
            source_delivery_job_id=None,
            source_candidate_sha="abc123integrated",
        )
        job = get_job(conn, job.id)
        prompt = build_role_prompt(job, workspace_path=str(repo))
    assert "source_candidate_sha: abc123integrated" in prompt
    assert "source_candidate_sha only" in prompt


def test_integration_success_binds_ready_release(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    _base, integrated = _commit_then_reset(repo, "feat.txt", "integrated")
    with connection(db) as conn:
        integ = create_job(
            conn,
            human_id="JOB-P2-INTEGRATION",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="INTEGRATION",
            queue="INTEGRATION",
            status="READY",
        )
        rel = create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            requires_worktree=False,
        )
        add_job_dependency(conn, rel.id, integ.id)
        integ = mark_succeeded(
            conn,
            integ.id,
            output_ref=None,
            candidate_git_sha=integrated,
        )
        bound = bind_dependent_release_jobs(conn, integ)
        rel = get_job(conn, rel.id)
    assert "JOB-P2-RELEASE" in bound
    assert rel.source_candidate_sha == integrated
    assert rel.source_delivery_job_id == integ.id


def test_release_worker_persists_integrated_sha_not_repo_head(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    head, integrated = _commit_then_reset(repo, "feat.txt", "integrated")
    assert current_head_sha(repo) == head
    assert integrated != head

    with connection(db) as conn:
        integ = create_job(
            conn,
            human_id="JOB-P2-INTEGRATION",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="INTEGRATION",
            queue="INTEGRATION",
            status="READY",
        )
        rel = create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            requires_worktree=False,
        )
        add_job_dependency(conn, rel.id, integ.id)
        mark_succeeded(
            conn,
            integ.id,
            output_ref=None,
            candidate_git_sha=integrated,
        )

    def _approve(conn, job, **kwargs):
        ev = tmp_path / "runs" / job.human_id
        ev.mkdir(parents=True, exist_ok=True)
        report = ev / "release-readiness.md"
        report.write_text("ok\n", encoding="utf-8")
        return ReleaseEvaluation(
            approved=True,
            reasons=[],
            candidate_sha=integrated,
            evidence_dir=ev,
            readiness_report_path=report,
            qa_package_path=None,
            release_human_id="REL-002",
            release_status="qa_passed",
            iteration_status="release_candidate",
            workspace_clean=True,
            workspace_head=integrated,
            outcome=GATE_READY_OUTCOME,
        )

    result = run_once(
        db_path=db,
        registry_path=cfg,
        job_human_id="JOB-P2-RELEASE",
        cursor_runner=make_cursor_runner(returncode=0, stdout="must not run"),
        skip_identity_validation=True,
        timeout_seconds=30,
        release_evaluator=_approve,
    )
    assert result.status == "succeeded"
    assert current_head_sha(repo) == head

    with connection(db) as conn:
        rel = get_job_by_human_id(conn, "JOB-P2-RELEASE")
        assert rel is not None
        assert rel.status == "SUCCEEDED"
        assert rel.source_candidate_sha == integrated
        assert rel.candidate_git_sha == integrated
        assert rel.candidate_git_sha != head


def test_release_worker_blocks_without_integrated_candidate(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            requires_worktree=False,
        )

    result = run_once(
        db_path=db,
        registry_path=cfg,
        job_human_id="JOB-P2-RELEASE",
        cursor_runner=make_cursor_runner(returncode=0, stdout="should not run"),
        skip_identity_validation=True,
        timeout_seconds=30,
    )
    assert result.status == "blocked"
    assert "integrated candidate" in result.message.lower()
    with connection(db) as conn:
        rel = get_job_by_human_id(conn, "JOB-P2-RELEASE")
        assert rel is not None
        assert rel.status == "BLOCKED"
        assert rel.candidate_git_sha is None
