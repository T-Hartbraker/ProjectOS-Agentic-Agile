"""CLI and governance tests for recover --reconcile-release."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

from projectos.cli import main
from projectos.db import connection
from projectos.qa_handoff import REQUIRED_ASSURANCE
from projectos.release_retry import (
    AUTHORITATIVE_INTEGRATION_SHA,
    reconcile_stale_release,
)
from projectos.store import (
    add_job_dependency,
    create_job,
    get_job_by_human_id,
    mark_succeeded,
)

from orch_helpers import init_git_repo, seed_db, write_registry

STALE_SHA = "56d580d2eca1a634a86990241d4da2958c3323ff"


def _cfg(tmp_path: Path, repo: Path) -> Path:
    return write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-003",
                "repository_root": str(repo.resolve()),
                "enabled": True,
            }
        ],
    )


def _insert_qa(conn, delivery, *, result: str = "pass") -> None:
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


def _seed_stale_release(
    db: Path,
    repo: Path,
    *,
    integration_status: str = "SUCCEEDED",
    integration_sha: str | None = AUTHORITATIVE_INTEGRATION_SHA,
    release_sha: str | None = STALE_SHA,
    qa_result: str | None = "pass",
    include_delivery: bool = True,
    extra_integration_sha: str | None = None,
    release_iteration: str = "ITER-002",
    release_project: str = "PRJ-003",
    release_attempt: int = 1,
) -> None:
    with connection(db) as conn:
        delivery = None
        if include_delivery:
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
            if qa_result is not None:
                _insert_qa(conn, delivery, result=qa_result)

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
        if delivery is not None:
            add_job_dependency(conn, integ.id, delivery.id)
        if integration_status == "SUCCEEDED":
            integ = mark_succeeded(
                conn,
                integ.id,
                output_ref=None,
                candidate_git_sha=integration_sha,
            )

        if extra_integration_sha is not None:
            extra = create_job(
                conn,
                human_id="JOB-P2-INTEGRATION-B",
                project_human_id="PRJ-003",
                repository_root=repo,
                agent_role="INTEGRATION",
                queue="INTEGRATION",
                status="READY",
                iteration_human_id="ITER-002",
            )
            mark_succeeded(
                conn,
                extra.id,
                output_ref=None,
                candidate_git_sha=extra_integration_sha,
            )

        rel = create_job(
            conn,
            human_id="JOB-P2-RELEASE",
            project_human_id=release_project,
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="READY",
            attempt=release_attempt,
            iteration_human_id=release_iteration,
            requires_worktree=True,
            worktree_name="PRJ-003__JOB-P2-RELEASE",
        )
        add_job_dependency(conn, rel.id, integ.id)
        if release_sha is not None:
            mark_succeeded(
                conn, rel.id, output_ref=None, candidate_git_sha=release_sha
            )


def test_recover_help_includes_reconcile_release() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["recover", "--help"])
    assert code == 0
    assert "--reconcile-release" in buf.getvalue()


def test_stale_release_creates_governed_successor(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    _seed_stale_release(db, repo)

    result = reconcile_stale_release(
        job_human_id="JOB-P2-RELEASE",
        db_path=db,
        registry_path=cfg,
    )
    assert result.ok
    assert result.successor_job_human_id == "JOB-P2-RELEASE__RETRY-1"
    assert result.successor_status in {"READY", "QUEUED"}
    assert result.source_candidate_sha == AUTHORITATIVE_INTEGRATION_SHA
    assert result.candidate_git_sha == STALE_SHA
    assert result.status == "SUCCEEDED"
    assert result.outcome == "SUPERSEDED"
    assert result.attempt == 1
    assert result.already_reconciled is False

    with connection(db) as conn:
        original = get_job_by_human_id(conn, "JOB-P2-RELEASE")
        successor = get_job_by_human_id(conn, "JOB-P2-RELEASE__RETRY-1")
        assert original is not None and successor is not None
        assert original.status == "SUCCEEDED"
        assert original.candidate_git_sha == STALE_SHA
        assert original.attempt == 1
        assert original.outcome == "SUPERSEDED"
        assert original.superseded_by_job_id == successor.id
        assert original.completed_at is not None

        events = [
            row[0]
            for row in conn.execute(
                "SELECT event_type FROM run_events WHERE job_id = ? ORDER BY id",
                (original.id,),
            )
        ]
        assert "job.succeeded" in events
        assert "release.stale_attempt_superseded" in events

        assert successor.queue == "RELEASE"
        assert successor.agent_role == "RELEASE"
        assert successor.project_human_id == "PRJ-003"
        assert successor.iteration_human_id == "ITER-002"
        assert successor.source_candidate_sha == AUTHORITATIVE_INTEGRATION_SHA
        assert successor.base_git_sha == AUTHORITATIVE_INTEGRATION_SHA
        assert successor.started_at is None
        assert successor.status in {"READY", "QUEUED"}
        running = conn.execute(
            """
            SELECT COUNT(*) FROM run_events
            WHERE job_id = ? AND event_type = 'job.running'
            """,
            (successor.id,),
        ).fetchone()[0]
        assert running == 0


def test_cli_reconcile_release_creates_successor(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    _seed_stale_release(db, repo)

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(
            [
                "--config",
                str(cfg),
                "recover",
                "--db",
                str(db),
                "--reconcile-release",
                "--job",
                "JOB-P2-RELEASE",
            ]
        )
    out = buf.getvalue()
    assert code == 0
    assert "JOB-P2-RELEASE__RETRY-1" in out
    assert AUTHORITATIVE_INTEGRATION_SHA in out
    assert "SUPERSEDED" in out


def test_blocks_when_integration_not_succeeded(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    _seed_stale_release(db, repo, integration_status="READY")

    result = reconcile_stale_release(
        job_human_id="JOB-P2-RELEASE", db_path=db, registry_path=cfg
    )
    assert not result.ok
    assert "integration is not successful" in result.message
    with connection(db) as conn:
        assert get_job_by_human_id(conn, "JOB-P2-RELEASE__RETRY-1") is None
        original = get_job_by_human_id(conn, "JOB-P2-RELEASE")
        assert original is not None
        assert original.superseded_by_job_id is None


def test_blocks_when_integration_sha_missing(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    _seed_stale_release(db, repo, integration_sha=None)

    result = reconcile_stale_release(
        job_human_id="JOB-P2-RELEASE", db_path=db, registry_path=cfg
    )
    assert not result.ok
    assert "integration candidate is missing" in result.message
    with connection(db) as conn:
        assert get_job_by_human_id(conn, "JOB-P2-RELEASE__RETRY-1") is None


def test_blocks_identity_mismatch(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    _seed_stale_release(db, repo, release_iteration="ITER-999")

    result = reconcile_stale_release(
        job_human_id="JOB-P2-RELEASE", db_path=db, registry_path=cfg
    )
    assert not result.ok
    assert "identity mismatch" in result.message
    with connection(db) as conn:
        assert get_job_by_human_id(conn, "JOB-P2-RELEASE__RETRY-1") is None


def test_blocks_incomplete_qa(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    _seed_stale_release(db, repo, qa_result=None)

    result = reconcile_stale_release(
        job_human_id="JOB-P2-RELEASE", db_path=db, registry_path=cfg
    )
    assert not result.ok
    assert "QA" in result.message
    with connection(db) as conn:
        assert get_job_by_human_id(conn, "JOB-P2-RELEASE__RETRY-1") is None
        original = get_job_by_human_id(conn, "JOB-P2-RELEASE")
        assert original is not None
        assert original.status == "SUCCEEDED"
        assert original.superseded_by_job_id is None


def test_blocks_when_no_stale_defect(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    _seed_stale_release(db, repo, release_sha=AUTHORITATIVE_INTEGRATION_SHA)

    result = reconcile_stale_release(
        job_human_id="JOB-P2-RELEASE", db_path=db, registry_path=cfg
    )
    assert not result.ok
    assert "no stale provenance defect" in result.message
    with connection(db) as conn:
        assert get_job_by_human_id(conn, "JOB-P2-RELEASE__RETRY-1") is None


def test_blocks_ambiguous_integration_candidates(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    cfg = _cfg(tmp_path, repo)
    _seed_stale_release(
        db,
        repo,
        extra_integration_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )

    result = reconcile_stale_release(
        job_human_id="JOB-P2-RELEASE", db_path=db, registry_path=cfg
    )
    assert not result.ok
    assert "ambiguous provenance" in result.message
    with connection(db) as conn:
        assert get_job_by_human_id(conn, "JOB-P2-RELEASE__RETRY-1") is None
