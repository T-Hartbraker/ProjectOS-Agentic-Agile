"""CLI and governance tests for recover --salvage-candidate."""

from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

from projectos.cli import main
from projectos.db import connection
from projectos.salvage import salvage_delivery_candidate
from projectos.store import (
    append_run_event,
    create_job,
    get_job_by_human_id,
    mark_cancelled,
    set_job_source_provenance,
)
from projectos.worktree import current_head_sha, ensure_worktree

from orch_helpers import init_git_repo, seed_db, write_registry


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


def _cfg(tmp_path: Path, repo: Path) -> Path:
    _write_identity(repo)
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


def _commit_file(repo: Path, rel: str, content: str, message: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return current_head_sha(repo)


def _seed_failed_delivery(
    db: Path,
    *,
    repo: Path,
    worktree: Path,
    base_sha: str,
    human_id: str = "JOB-P2-DEL-DUE-OVERDUE__REWORK-1",
    attempt: int = 3,
) -> None:
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id=human_id,
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            iteration_human_id="ITER-002",
            work_item_type="story",
            work_item_human_id="US-007",
            max_attempts=3,
            attempt=attempt,
            requires_worktree=True,
            worktree_name=f"PRJ-003__{human_id}",
            worktree_path=str(worktree),
            base_git_sha=base_sha,
            identity_snapshot={
                "project_human_id": "PRJ-003",
                "repository_root": str(repo.resolve()),
            },
        )
        conn.execute(
            """
            UPDATE orchestration_jobs
            SET status = 'FAILED',
                last_error = 'lease expired while RUNNING; max attempts exhausted (3/3)',
                completed_at = '2026-08-21T17:17:45Z',
                updated_at = '2026-08-21T17:17:45Z'
            WHERE id = ?
            """,
            (job.id,),
        )
        append_run_event(
            conn,
            job.id,
            "job.running",
            status="RUNNING",
            message="attempt started",
        )
        append_run_event(
            conn,
            job.id,
            "lease.expired_recovered",
            status="FAILED",
            message="lease expired while RUNNING; max attempts exhausted (3/3)",
        )
        append_run_event(
            conn,
            job.id,
            "lease.reclaim_requested",
            status="RUNNING",
            message="governed reclaim after interrupted Cursor worker",
        )


def _seed_stale_assurance(db: Path, repo: Path, stale_sha: str) -> None:
    with connection(db) as conn:
        delivery = create_job(
            conn,
            human_id="JOB-P2-DEL-DUE-OVERDUE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            base_git_sha=stale_sha,
            identity_snapshot={
                "project_human_id": "PRJ-003",
                "repository_root": str(repo.resolve()),
            },
        )
        conn.execute(
            """
            UPDATE orchestration_jobs
            SET status = 'SUCCEEDED',
                candidate_git_sha = ?,
                outcome = 'INVALIDATED',
                completed_at = '2026-08-21T12:00:00Z'
            WHERE id = ?
            """,
            (stale_sha, delivery.id),
        )
        for queue in (
            "ASSURANCE_FUNCTIONAL",
            "ASSURANCE_INTEGRATION",
            "ASSURANCE_SECURITY",
            "ASSURANCE_QUALITY",
        ):
            qa = create_job(
                conn,
                human_id=f"JOB-P2-DEL-DUE-OVERDUE__{queue}",
                project_human_id="PRJ-003",
                repository_root=repo,
                agent_role=queue,
                queue=queue,
                status="READY",
                base_git_sha=stale_sha,
                identity_snapshot={
                    "project_human_id": "PRJ-003",
                    "repository_root": str(repo.resolve()),
                },
            )
            set_job_source_provenance(
                conn,
                qa.id,
                source_delivery_job_id=delivery.id,
                source_candidate_sha=stale_sha,
            )
            mark_cancelled(
                conn,
                qa.id,
                reason="stale noop candidate invalidated",
            )


def test_recover_help_includes_salvage_candidate() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["recover", "--help"])
    assert code == 0
    assert "--salvage-candidate" in buf.getvalue()


def test_salvage_success_preserves_failure_history_and_binds_qa(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path / "PersonalTaskManager")
    base = current_head_sha(repo)
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")

    wt_info = ensure_worktree(
        repo,
        name="PRJ-003__JOB-P2-DEL-DUE-OVERDUE__REWORK-1",
        base_ref=base,
    )
    candidate = _commit_file(
        wt_info.path,
        "due.py",
        "due = True\n",
        "feat: due date candidate",
    )
    assert candidate != base
    assert candidate.startswith(candidate[:7])

    _seed_stale_assurance(db, repo, base)
    _seed_failed_delivery(db, repo=repo, worktree=wt_info.path, base_sha=base)

    result = salvage_delivery_candidate(
        job_human_id="JOB-P2-DEL-DUE-OVERDUE__REWORK-1",
        db_path=db,
        registry_path=cfg,
    )
    assert result.ok
    assert result.status == "SUCCEEDED"
    assert result.outcome == "SALVAGED"
    assert result.attempt == 3
    assert result.candidate_git_sha == candidate
    assert result.assurance_job_ids

    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-P2-DEL-DUE-OVERDUE__REWORK-1")
        assert job is not None
        assert job.status == "SUCCEEDED"
        assert job.outcome == "SALVAGED"
        assert job.attempt == 3
        assert job.candidate_git_sha == candidate
        assert job.base_git_sha == base

        events = [
            r[0]
            for r in conn.execute(
                "SELECT event_type FROM run_events WHERE job_id = ? ORDER BY id",
                (job.id,),
            )
        ]
        assert "lease.expired_recovered" in events
        assert "lease.reclaim_requested" in events
        assert "delivery.candidate_salvaged" in events
        # Must not rewrite history as a normal worker success.
        assert "job.succeeded" not in events

        for hid in result.assurance_job_ids:
            qa = get_job_by_human_id(conn, hid)
            assert qa is not None
            assert qa.status == "READY"
            assert qa.source_candidate_sha == candidate
            assert qa.source_delivery_job_id == job.id
            if qa.requires_worktree:
                assert qa.base_git_sha == candidate
                assert qa.human_id.endswith(
                    tuple(
                        f"__{q}"
                        for q in (
                            "ASSURANCE_FUNCTIONAL",
                            "ASSURANCE_INTEGRATION",
                            "ASSURANCE_SECURITY",
                            "ASSURANCE_QUALITY",
                        )
                    )
                )

        for queue in (
            "ASSURANCE_FUNCTIONAL",
            "ASSURANCE_INTEGRATION",
            "ASSURANCE_SECURITY",
            "ASSURANCE_QUALITY",
        ):
            stale = get_job_by_human_id(
                conn, f"JOB-P2-DEL-DUE-OVERDUE__{queue}"
            )
            assert stale is not None
            assert stale.status == "CANCELLED"
            assert stale.source_candidate_sha == base


def test_salvage_cli_success(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    base = current_head_sha(repo)
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    wt = ensure_worktree(repo, name="PRJ-003__JOB-SALVAGE-CLI", base_ref=base)
    candidate = _commit_file(wt.path, "x.txt", "x\n", "candidate")
    _seed_failed_delivery(
        db,
        repo=repo,
        worktree=wt.path,
        base_sha=base,
        human_id="JOB-SALVAGE-CLI",
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(
            [
                "--config",
                str(cfg),
                "recover",
                "--db",
                str(db),
                "--salvage-candidate",
                "--job",
                "JOB-SALVAGE-CLI",
            ]
        )
    out = buf.getvalue()
    assert code == 0
    assert "outcome: SALVAGED" in out
    assert candidate in out
    assert "assurance_jobs:" in out


def test_salvage_blocks_dirty_worktree(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    base = current_head_sha(repo)
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    wt = ensure_worktree(repo, name="PRJ-003__JOB-DIRTY", base_ref=base)
    _commit_file(wt.path, "c.txt", "c\n", "candidate")
    (wt.path / "dirty.txt").write_text("nope\n", encoding="utf-8")
    _seed_failed_delivery(
        db, repo=repo, worktree=wt.path, base_sha=base, human_id="JOB-DIRTY"
    )

    result = salvage_delivery_candidate(
        job_human_id="JOB-DIRTY", db_path=db, registry_path=cfg
    )
    assert not result.ok
    assert "dirty" in result.message.lower()


def test_salvage_blocks_head_equals_base(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    base = current_head_sha(repo)
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    wt = ensure_worktree(repo, name="PRJ-003__JOB-NOOP", base_ref=base)
    _seed_failed_delivery(
        db, repo=repo, worktree=wt.path, base_sha=base, human_id="JOB-NOOP"
    )

    result = salvage_delivery_candidate(
        job_human_id="JOB-NOOP", db_path=db, registry_path=cfg
    )
    assert not result.ok
    assert "equals base" in result.message.lower()


def test_salvage_blocks_non_descendant_head(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    base = current_head_sha(repo)
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")

    # Divergent history: orphan commit not descending from base.
    subprocess.run(
        ["git", "checkout", "--orphan", "orphan-branch"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "rm", "-rf", "."], cwd=repo, check=False, capture_output=True)
    (repo / "orphan.txt").write_text("orphan\n", encoding="utf-8")
    subprocess.run(["git", "add", "orphan.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "orphan"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    orphan = current_head_sha(repo)
    subprocess.run(
        ["git", "checkout", "-B", "main", base],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    wt_path = (repo.parent / f"{repo.name}.worktrees" / "PRJ-003__JOB-ORPHAN").resolve()
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", str(wt_path), orphan],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    _seed_failed_delivery(
        db, repo=repo, worktree=wt_path, base_sha=base, human_id="JOB-ORPHAN"
    )

    result = salvage_delivery_candidate(
        job_human_id="JOB-ORPHAN", db_path=db, registry_path=cfg
    )
    assert not result.ok
    assert "descend" in result.message.lower()


def test_salvage_blocks_unknown_worktree(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    base = current_head_sha(repo)
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    fake = tmp_path / "not-a-registered-worktree"
    fake.mkdir()
    subprocess.run(["git", "init"], cwd=fake, check=True, capture_output=True)
    _seed_failed_delivery(
        db, repo=repo, worktree=fake, base_sha=base, human_id="JOB-UNKNOWN-WT"
    )

    result = salvage_delivery_candidate(
        job_human_id="JOB-UNKNOWN-WT", db_path=db, registry_path=cfg
    )
    assert not result.ok
    assert "not registered" in result.message.lower()


def test_salvage_blocks_repository_identity_mismatch(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    other = init_git_repo(tmp_path / "other")
    base = current_head_sha(repo)
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    wt = ensure_worktree(repo, name="PRJ-003__JOB-MISMATCH", base_ref=base)
    _commit_file(wt.path, "m.txt", "m\n", "candidate")
    _seed_failed_delivery(
        db,
        repo=other,  # wrong repo on job vs registry PRJ-003 -> repo
        worktree=wt.path,
        base_sha=base,
        human_id="JOB-MISMATCH",
    )
    # Force project id PRJ-003 with wrong repository_root already set via create_job.
    with connection(db) as conn:
        conn.execute(
            """
            UPDATE orchestration_jobs
            SET project_human_id = 'PRJ-003',
                repository_root = ?
            WHERE human_id = 'JOB-MISMATCH'
            """,
            (str(other.resolve()),),
        )

    result = salvage_delivery_candidate(
        job_human_id="JOB-MISMATCH", db_path=db, registry_path=cfg
    )
    assert not result.ok
    assert "identity mismatch" in result.message.lower()


def test_salvage_blocks_active_worker(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    base = current_head_sha(repo)
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    wt = ensure_worktree(repo, name="PRJ-003__JOB-ACTIVE", base_ref=base)
    _commit_file(wt.path, "a.txt", "a\n", "candidate")
    _seed_failed_delivery(
        db, repo=repo, worktree=wt.path, base_sha=base, human_id="JOB-ACTIVE"
    )
    with connection(db) as conn:
        create_job(
            conn,
            human_id="JOB-HOLDER",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="RUNNING",
            requires_worktree=True,
            worktree_name="PRJ-003__JOB-ACTIVE",
            worktree_path=str(wt.path),
            base_git_sha=base,
            identity_snapshot={
                "project_human_id": "PRJ-003",
                "repository_root": str(repo.resolve()),
            },
        )

    result = salvage_delivery_candidate(
        job_human_id="JOB-ACTIVE", db_path=db, registry_path=cfg
    )
    assert not result.ok
    assert "owns worktree" in result.message.lower()
