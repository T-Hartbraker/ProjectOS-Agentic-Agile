"""Write-time isolation: every linker rejects cross-project rows transactionally."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity
from projectos.db import connection
from projectos.errors import CrossProjectWriteError
from projectos.integration import integrate_candidates
from projectos.invalidate import (
    invalidate_delivery_candidate,
    rewire_integration_dependencies,
)
from projectos.migrate import initialize_database
from projectos.qa_handoff import create_assurance_jobs_for_delivery
from projectos.recover import run_recovery
from projectos.store import (
    add_job_dependency,
    append_run_event,
    create_job,
    get_job,
    get_job_by_human_id,
    insert_agent_run,
    insert_candidate_invalidation,
    insert_integration_run,
    insert_qa_evidence,
    set_job_outcome,
    set_job_source_provenance,
)


def _sha(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return db


def _pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo_a = init_git_repo(tmp_path / "repo-a")
    repo_b = init_git_repo(tmp_path / "repo-b")
    write_identity(repo_a, project_human_id="PRJ-A")
    write_identity(repo_b, project_human_id="PRJ-B")
    return _db(tmp_path), repo_a, repo_b


def _job(conn, *, human_id: str, project: str, repo: Path, queue: str = "PM"):
    return create_job(
        conn,
        human_id=human_id,
        project_human_id=project,
        repository_root=repo,
        agent_role=queue if queue != "DELIVERY" else "DELIVERY",
        queue=queue,
        status="READY",
        identity_snapshot={
            "project_human_id": project,
            "repository_root": str(repo),
        },
    )


def test_dependency_and_create_job_reject_cross_project(tmp_path: Path) -> None:
    db, repo_a, repo_b = _pair(tmp_path)
    with connection(db) as conn:
        a = _job(conn, human_id="JOB-A", project="PRJ-A", repo=repo_a)
        b = _job(conn, human_id="JOB-B", project="PRJ-B", repo=repo_b)
        with pytest.raises(CrossProjectWriteError, match="add_job_dependency"):
            add_job_dependency(conn, a.id, b.id)
        with pytest.raises(CrossProjectWriteError, match="identity_snapshot"):
            create_job(
                conn,
                human_id="JOB-BAD-ID",
                project_human_id="PRJ-A",
                repository_root=repo_a,
                agent_role="PM",
                queue="PM",
                identity_snapshot={"project_human_id": "PRJ-B"},
            )
        with pytest.raises(CrossProjectWriteError, match="already belongs"):
            create_job(
                conn,
                human_id="JOB-STEAL",
                project_human_id="PRJ-C",
                repository_root=repo_b,
                agent_role="PM",
                queue="PM",
            )
        with pytest.raises(CrossProjectWriteError, match="is bound to"):
            create_job(
                conn,
                human_id="JOB-MOVED",
                project_human_id="PRJ-B",
                repository_root=repo_a,
                agent_role="PM",
                queue="PM",
            )
    with connection(db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM orchestration_job_dependencies"
        ).fetchone()[0]
        assert int(n) == 0


def test_dependency_failure_rolls_back_open_transaction(tmp_path: Path) -> None:
    db, repo_a, repo_b = _pair(tmp_path)
    with pytest.raises(CrossProjectWriteError):
        with connection(db) as conn:
            a = _job(conn, human_id="JOB-A", project="PRJ-A", repo=repo_a)
            b = _job(conn, human_id="JOB-B", project="PRJ-B", repo=repo_b)
            add_job_dependency(conn, a.id, b.id)
    with connection(db) as conn:
        assert get_job_by_human_id(conn, "JOB-A") is None
        assert get_job_by_human_id(conn, "JOB-B") is None
        n = conn.execute(
            "SELECT COUNT(*) FROM orchestration_job_dependencies"
        ).fetchone()[0]
        assert int(n) == 0


def test_assurance_children_and_qa_evidence_stay_on_project(tmp_path: Path) -> None:
    db, repo_a, repo_b = _pair(tmp_path)
    with connection(db) as conn:
        delivery = _job(
            conn, human_id="DEL-A", project="PRJ-A", repo=repo_a, queue="DELIVERY"
        )
        other = _job(
            conn, human_id="DEL-B", project="PRJ-B", repo=repo_b, queue="DELIVERY"
        )
        result = create_assurance_jobs_for_delivery(
            conn, delivery, candidate_git_sha=_sha(repo_a)
        )
        assert result.assurance_job_ids
        child = get_job_by_human_id(conn, result.assurance_job_ids[0])
        assert child is not None
        assert child.project_human_id == "PRJ-A"
        with pytest.raises(CrossProjectWriteError, match="add_job_dependency"):
            add_job_dependency(conn, child.id, other.id)
        with pytest.raises(CrossProjectWriteError, match="insert_qa_evidence"):
            insert_qa_evidence(
                conn,
                project_human_id="PRJ-A",
                repository_root=repo_a,
                delivery_job_id=delivery.id,
                assurance_job_id=other.id,
                candidate_git_sha=_sha(repo_a),
                assurance_role="ASSURANCE_FUNCTIONAL",
            )


def test_rework_invalidation_and_integration_rewire_reject_foreign_jobs(
    tmp_path: Path,
) -> None:
    db, repo_a, repo_b = _pair(tmp_path)
    with connection(db) as conn:
        delivery = _job(
            conn, human_id="DEL-A", project="PRJ-A", repo=repo_a, queue="DELIVERY"
        )
        foreign = _job(
            conn, human_id="DEL-B", project="PRJ-B", repo=repo_b, queue="DELIVERY"
        )
        conn.execute(
            "UPDATE orchestration_jobs SET candidate_git_sha = ? WHERE id = ?",
            (_sha(repo_a), delivery.id),
        )
        delivery_id = delivery.id
        foreign_id = foreign.id
        delivery_hid = delivery.human_id
        foreign_hid = foreign.human_id

    with pytest.raises(CrossProjectWriteError, match="add_job_dependency"):
        with connection(db) as conn:
            invalidate_delivery_candidate(
                conn,
                delivery_hid,
                reason="bad candidate",
                rework_human_id="DEL-A__REWORK",
                work_item_type="story",
                work_item_human_id="STORY-1",
                depend_on_human_ids=[foreign_hid],
            )

    with connection(db) as conn:
        assert get_job_by_human_id(conn, "DEL-A__REWORK") is None
        n = conn.execute("SELECT COUNT(*) FROM candidate_invalidations").fetchone()[0]
        assert int(n) == 0
        with pytest.raises(CrossProjectWriteError, match="insert_candidate_invalidation"):
            insert_candidate_invalidation(
                conn,
                delivery_job_id=delivery_id,
                invalidated_candidate_sha=_sha(repo_a),
                reason="mix",
                rework_job_id=foreign_id,
            )
        integ = _job(
            conn, human_id="INT-A", project="PRJ-A", repo=repo_a, queue="INTEGRATION"
        )
        rework_b = _job(
            conn, human_id="DEL-B-R", project="PRJ-B", repo=repo_b, queue="DELIVERY"
        )
        add_job_dependency(conn, integ.id, delivery_id)
        integ_hid = integ.human_id
        rework_b_hid = rework_b.human_id

    with pytest.raises(CrossProjectWriteError, match="add_job_dependency"):
        with connection(db) as conn:
            rewire_integration_dependencies(
                conn,
                integration_human_id=integ_hid,
                replace_deps={delivery_hid: rework_b_hid},
            )


def test_integration_run_and_release_retry_linkers_reject_foreign_jobs(
    tmp_path: Path,
) -> None:
    db, repo_a, repo_b = _pair(tmp_path)
    with connection(db) as conn:
        a = _job(
            conn, human_id="DEL-A", project="PRJ-A", repo=repo_a, queue="DELIVERY"
        )
        b = _job(
            conn, human_id="DEL-B", project="PRJ-B", repo=repo_b, queue="DELIVERY"
        )
        release = _job(
            conn, human_id="REL-A", project="PRJ-A", repo=repo_a, queue="RELEASE"
        )
        integ_b = _job(
            conn, human_id="INT-B", project="PRJ-B", repo=repo_b, queue="INTEGRATION"
        )
        with pytest.raises(CrossProjectWriteError, match="insert_integration_run"):
            insert_integration_run(
                conn,
                project_human_id="PRJ-A",
                repository_root=repo_a,
                iteration_human_id="ITER-1",
                source_job_ids=[a.id, b.id],
                source_shas=[_sha(repo_a), _sha(repo_b)],
            )
        with pytest.raises(CrossProjectWriteError, match="add_job_dependency"):
            add_job_dependency(conn, release.id, integ_b.id)
        with pytest.raises(CrossProjectWriteError, match="set_job_source_provenance"):
            set_job_source_provenance(
                conn,
                release.id,
                source_delivery_job_id=b.id,
                source_candidate_sha=_sha(repo_b),
            )
        with pytest.raises(CrossProjectWriteError, match="set_job_outcome"):
            set_job_outcome(
                conn,
                a.id,
                outcome="SUPERSEDED",
                superseded_by_job_id=b.id,
            )
    with pytest.raises(CrossProjectWriteError, match="insert_integration_run"):
        integrate_candidates(
            repository_root=repo_a,
            project_human_id="PRJ-A",
            source_shas=[_sha(repo_a)],
            source_job_ids=[a.id, b.id],
            db_path=db,
        )
    with connection(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM integration_runs").fetchone()[0]
        assert int(n) == 0


def test_learning_lineage_and_artifact_refs_cannot_cross_projects(
    tmp_path: Path,
) -> None:
    db, repo_a, repo_b = _pair(tmp_path)
    with connection(db) as conn:
        a = _job(conn, human_id="JOB-A", project="PRJ-A", repo=repo_a)
        b = _job(conn, human_id="JOB-B", project="PRJ-B", repo=repo_b)
        with pytest.raises(CrossProjectWriteError, match="append_run_event"):
            append_run_event(
                conn,
                a.id,
                "job.note",
                payload={"project_human_id": "PRJ-B"},
            )
        insert_agent_run(
            conn,
            job_id=a.id,
            worker_id="w1",
            cursor_command=["agent"],
            prompt_ref=None,
            output_ref=None,
            stdout_ref=None,
            stderr_ref=None,
            exit_code=0,
            started_at="2026-01-01T00:00:00Z",
            ended_at="2026-01-01T00:00:01Z",
            duration_ms=1000,
            worktree_name=None,
            worktree_path=None,
            base_git_sha=None,
            candidate_git_sha="abc",
            dirty=False,
            usage=None,
            error=None,
        )
        with pytest.raises(CrossProjectWriteError):
            insert_qa_evidence(
                conn,
                project_human_id="PRJ-B",
                repository_root=repo_b,
                delivery_job_id=a.id,
                assurance_job_id=a.id,
                candidate_git_sha="abc",
                assurance_role="ASSURANCE_FUNCTIONAL",
            )
        n = conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
        assert int(n) == 0
        runs = conn.execute("SELECT job_id FROM agent_runs").fetchall()
        assert [int(r[0]) for r in runs] == [a.id]
        assert get_job(conn, b.id).project_human_id == "PRJ-B"


def test_recovery_does_not_rebind_job_to_another_project(tmp_path: Path) -> None:
    db, repo_a, repo_b = _pair(tmp_path)
    from helpers import write_registry
    from helpers import fake_status

    write_identity(repo_a, project_human_id="PRJ-A", project_name="A")
    write_identity(repo_b, project_human_id="PRJ-B", project_name="B")
    cfg = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-A",
                "repository_root": str(repo_a.resolve()),
                "enabled": True,
            },
            {
                "project_human_id": "PRJ-B",
                "repository_root": str(repo_b.resolve()),
                "enabled": True,
            },
        ],
    )
    with connection(db) as conn:
        a = _job(conn, human_id="JOB-A", project="PRJ-A", repo=repo_a)
        b = _job(conn, human_id="JOB-B", project="PRJ-B", repo=repo_b)
    report = run_recovery(
        db_path=db,
        registry_path=cfg,
        projectctl_runner=lambda root: fake_status(
            "PRJ-A" if Path(root).resolve() == repo_a.resolve() else "PRJ-B"
        ),
    )
    assert report is not None
    with connection(db) as conn:
        assert get_job(conn, a.id).project_human_id == "PRJ-A"
        assert get_job(conn, b.id).project_human_id == "PRJ-B"
        assert get_job(conn, a.id).repository_root
        n = conn.execute(
            "SELECT COUNT(*) FROM orchestration_job_dependencies"
        ).fetchone()[0]
        assert int(n) == 0
