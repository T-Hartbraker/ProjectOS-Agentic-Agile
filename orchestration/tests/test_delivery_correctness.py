"""Delivery correctness: work-item context, candidate SHA, QA handoff, cancel."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from projectos.cli import main
from projectos.db import connection
from projectos.delivery_evidence import evaluate_delivery_candidate, is_valid_qa_candidate
from projectos.dispatch import run_dispatch
from projectos.invalidate import (
    invalidate_delivery_candidate,
    reconcile_prj003_iter002_fat,
)
from projectos.plan import validate_plan_document
from projectos.projectctl_bridge import ProjectctlStatusResult
from projectos.prompt_builder import build_role_prompt, resolve_delivery_assignment
from projectos.qa_handoff import create_assurance_jobs_for_delivery, maybe_handoff_after_delivery
from projectos.store import (
    OrchestrationJob,
    add_job_dependency,
    create_job,
    dependencies_satisfied,
    get_job,
    get_job_by_human_id,
    mark_succeeded,
)
from projectos.worker import run_once
from projectos.worktree import current_head_sha

from orch_helpers import (
    FakeCompletedProcess,
    init_git_repo,
    make_cursor_runner,
    seed_db,
    write_registry,
)


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


def _assignment(**kwargs):
    base = {
        "requirement_ref": "story:US-TEST",
        "title": "Test story",
        "acceptance_criteria": [
            "AC-001: Given X when Y then Z",
            "AC-002: Preserve prior behavior",
        ],
        "definition_of_ready": ["Story ready"],
        "definition_of_done": ["Candidate committed"],
        "expected_implementation_evidence": ["candidate != base"],
    }
    base.update(kwargs)
    return base


def test_delivery_plan_rejects_missing_work_item() -> None:
    errors = validate_plan_document(
        {
            "schema_version": 1,
            "project_human_id": "PRJ-003",
            "sponsor_authority": "approved",
            "jobs": [
                {
                    "human_id": "JOB-DEL",
                    "queue": "DELIVERY",
                    "agent_role": "DELIVERY",
                    "depends_on": [],
                }
            ],
        },
        expected_project_id="PRJ-003",
    )
    assert any("work_item" in e or "acceptance_criteria" in e for e in errors)


def test_worker_prompt_contains_acceptance_criteria(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-DEL-PROMPT",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            assignment=_assignment(),
            work_item_type="story",
            work_item_human_id="US-TEST",
        )
        resolved = resolve_delivery_assignment(job, repository_root=repo)
        prompt = build_role_prompt(
            job,
            workspace_path=str(repo),
            base_git_sha="abc123",
            resolved=resolved,
        )
    assert "AC-001: Given X when Y then Z" in prompt
    assert "base_git_sha: abc123" in prompt
    assert "US-TEST" in prompt


def test_cursor_exit_0_unchanged_sha_is_not_delivery_success(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    base = current_head_sha(repo)
    with connection(db) as conn:
        create_job(
            conn,
            human_id="JOB-DEL-NOOP",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            requires_worktree=True,
            base_git_sha=base,
            assignment=_assignment(),
        )
    result = run_once(
        db_path=db,
        registry_path=cfg,
        job_human_id="JOB-DEL-NOOP",
        cursor_runner=make_cursor_runner(returncode=0, stdout="done"),
        skip_identity_validation=True,
        timeout_seconds=30,
    )
    assert result.status != "succeeded"
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-DEL-NOOP")
        assert job is not None
        assert job.status in {"RETRY_WAIT", "FAILED", "BLOCKED"}
        assert conn.execute(
            "SELECT COUNT(*) FROM qa_evidence WHERE delivery_job_id = ?",
            (job.id,),
        ).fetchone()[0] == 0


def test_valid_code_changing_delivery_produces_different_candidate(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    base = current_head_sha(repo)

    def mutating_runner(cmd, **kwargs):
        # Cursor writes into the worktree cwd.
        cwd = Path(kwargs.get("cwd") or repo)
        (cwd / "feature.txt").write_text("implemented", encoding="utf-8")
        return FakeCompletedProcess(0, "implemented feature", "")

    with connection(db) as conn:
        create_job(
            conn,
            human_id="JOB-DEL-OK",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            requires_worktree=True,
            base_git_sha=base,
            assignment=_assignment(),
        )
    result = run_once(
        db_path=db,
        registry_path=cfg,
        job_human_id="JOB-DEL-OK",
        cursor_runner=mutating_runner,
        skip_identity_validation=True,
        timeout_seconds=30,
    )
    assert result.status == "succeeded"
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-DEL-OK")
        assert job is not None
        assert job.candidate_git_sha
        assert job.base_git_sha
        assert job.candidate_git_sha != job.base_git_sha
        assert is_valid_qa_candidate(job)
        assert conn.execute(
            "SELECT COUNT(*) FROM qa_evidence WHERE delivery_job_id = ?",
            (job.id,),
        ).fetchone()[0] >= 4
        run = conn.execute(
            "SELECT base_git_sha, candidate_git_sha, worktree_path FROM agent_runs "
            "WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (job.id,),
        ).fetchone()
        assert run["base_git_sha"] == job.base_git_sha
        assert run["candidate_git_sha"] == job.candidate_git_sha
        assert run["worktree_path"]


def test_qa_handoff_not_created_for_noop_candidate(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    sha = "same-sha"
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-DEL-H1",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="RUNNING",
            base_git_sha=sha,
        )
        mark_succeeded(conn, job.id, output_ref=None, candidate_git_sha=sha)
        job = get_job(conn, job.id)
        assert maybe_handoff_after_delivery(conn, job) is None
        with pytest.raises(Exception, match="no-op|equals"):
            create_assurance_jobs_for_delivery(conn, job, candidate_git_sha=sha)


def test_qa_handoff_created_for_valid_candidate(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-DEL-H2",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="RUNNING",
            base_git_sha="base-sha",
        )
        mark_succeeded(conn, job.id, output_ref=None, candidate_git_sha="cand-sha")
        job = get_job(conn, job.id)
        handoff = maybe_handoff_after_delivery(conn, job)
        assert handoff is not None
        assert handoff.candidate_git_sha == "cand-sha"
        assert len(handoff.assurance_job_ids) >= 5


def test_stale_assurance_cannot_approve_invalidated(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        delivery = create_job(
            conn,
            human_id="JOB-P2-DEL-DUE-OVERDUE",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
            base_git_sha="base",
        )
        mark_succeeded(
            conn, delivery.id, output_ref=None, candidate_git_sha="noop-sha"
        )
        conn.execute(
            "UPDATE orchestration_jobs SET base_git_sha=?, candidate_git_sha=? WHERE id=?",
            ("base", "noop-sha", delivery.id),
        )
        # Seed ARCH dep target
        create_job(
            conn,
            human_id="JOB-P2-ARCH",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="ARCHITECTURE",
            queue="ARCHITECTURE",
            status="SUCCEEDED",
        )
        create_job(
            conn,
            human_id="JOB-P2-INTEGRATION",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="INTEGRATION",
            queue="INTEGRATION",
            status="READY",
        )
        add_job_dependency(
            conn,
            get_job_by_human_id(conn, "JOB-P2-INTEGRATION").id,
            delivery.id,
        )
        # Pretend assurance existed
        create_job(
            conn,
            human_id="JOB-P2-DEL-DUE-OVERDUE__ASSURANCE_FUNCTIONAL",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="ASSURANCE_FUNCTIONAL",
            queue="ASSURANCE_FUNCTIONAL",
            status="READY",
        )
        conn.execute(
            """
            UPDATE orchestration_jobs
            SET source_delivery_job_id = ?, source_candidate_sha = ?
            WHERE human_id = ?
            """,
            (
                delivery.id,
                "noop-sha",
                "JOB-P2-DEL-DUE-OVERDUE__ASSURANCE_FUNCTIONAL",
            ),
        )
        inv = invalidate_delivery_candidate(
            conn,
            "JOB-P2-DEL-DUE-OVERDUE",
            reason="test invalidate",
            rework_human_id="JOB-P2-DEL-DUE-OVERDUE__REWORK-1",
            work_item_type="story",
            work_item_human_id="US-007",
            assignment=_assignment(title="Due date"),
            depend_on_human_ids=["JOB-P2-ARCH"],
        )
        assert inv.invalidated
        delivery = get_job_by_human_id(conn, "JOB-P2-DEL-DUE-OVERDUE")
        assert delivery.outcome == "INVALIDATED"
        # Historical SUCCEEDED preserved
        assert delivery.status == "SUCCEEDED"
        # Events remain
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM run_events WHERE job_id = ?", (delivery.id,)
            ).fetchone()[0]
            >= 1
        )
        assurance = get_job_by_human_id(
            conn, "JOB-P2-DEL-DUE-OVERDUE__ASSURANCE_FUNCTIONAL"
        )
        assert assurance.status == "CANCELLED"
        # Invalidated delivery does not satisfy INTEGRATION deps
        integ = get_job_by_human_id(conn, "JOB-P2-INTEGRATION")
        assert not dependencies_satisfied(conn, integ.id)
        rework = get_job_by_human_id(conn, "JOB-P2-DEL-DUE-OVERDUE__REWORK-1")
        assert rework is not None
        assert rework.work_item_human_id == "US-007"
        assert rework.status == "READY"


def test_dependency_semantics_hold_with_outcomes(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        a = create_job(
            conn,
            human_id="DEP-A",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
        )
        b = create_job(
            conn,
            human_id="DEP-B",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="INTEGRATION",
            queue="INTEGRATION",
            status="READY",
        )
        add_job_dependency(conn, b.id, a.id)
        assert dependencies_satisfied(conn, b.id)
        conn.execute(
            "UPDATE orchestration_jobs SET outcome='NO_CHANGE' WHERE id=?",
            (a.id,),
        )
        assert not dependencies_satisfied(conn, b.id)


def test_dispatch_cancellation_is_bounded(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")

    def hanging_runner(cmd, **kwargs):
        time.sleep(60)
        return FakeCompletedProcess(0, "late", "")

    with connection(db) as conn:
        create_job(
            conn,
            human_id="JOB-HANG",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="PM",
            queue="PM",
            status="READY",
        )

    cancel = threading.Event()

    def cancel_soon():
        time.sleep(0.2)
        cancel.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.perf_counter()
    result = run_dispatch(
        until_idle=True,
        max_parallel=1,
        db_path=db,
        registry_path=cfg,
        cursor_runner=hanging_runner,
        skip_identity_validation=True,
        timeout_seconds=30,
        cancel_event=cancel,
        wait_cycle_seconds=0.2,
        shutdown_grace_seconds=2.0,
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 10.0
    assert result.cancelled or result.exit_code == 130


def test_worker_timeout_never_returning(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")

    def hanging_runner(cmd, **kwargs):
        time.sleep(30)
        return FakeCompletedProcess(0, "late", "")

    with connection(db) as conn:
        create_job(
            conn,
            human_id="JOB-TO",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="PM",
            queue="PM",
            status="READY",
        )
    started = time.perf_counter()
    result = run_once(
        db_path=db,
        registry_path=cfg,
        job_human_id="JOB-TO",
        cursor_runner=hanging_runner,
        skip_identity_validation=True,
        timeout_seconds=0.3,
    )
    assert time.perf_counter() - started < 5.0
    assert result.exit_code != 0


def test_fat_reconcile_preserves_audit_history(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        for hid in (
            "JOB-P2-PM-SETUP",
            "JOB-P2-ARCH",
            "JOB-P2-DEL-DUE-OVERDUE",
            "JOB-P2-DEL-PRIORITY-FILTER",
            "JOB-P2-INTEGRATION",
            "JOB-P2-RELEASE",
        ):
            queue = {
                "JOB-P2-PM-SETUP": ("PM", "PM"),
                "JOB-P2-ARCH": ("ARCHITECTURE", "ARCHITECTURE"),
                "JOB-P2-DEL-DUE-OVERDUE": ("DELIVERY", "DELIVERY"),
                "JOB-P2-DEL-PRIORITY-FILTER": ("DELIVERY", "DELIVERY"),
                "JOB-P2-INTEGRATION": ("INTEGRATION", "INTEGRATION"),
                "JOB-P2-RELEASE": ("RELEASE", "RELEASE"),
            }[hid]
            status = (
                "READY"
                if hid in {"JOB-P2-INTEGRATION", "JOB-P2-RELEASE"}
                else "SUCCEEDED"
            )
            create_job(
                conn,
                human_id=hid,
                project_human_id="PRJ-003",
                repository_root=repo,
                agent_role=queue[0],
                queue=queue[1],
                status=status,
                base_git_sha="56d580d2eca1a634a86990241d4da2958c3323ff",
            )
        for hid in (
            "JOB-P2-DEL-DUE-OVERDUE",
            "JOB-P2-DEL-PRIORITY-FILTER",
        ):
            job = get_job_by_human_id(conn, hid)
            conn.execute(
                """
                UPDATE orchestration_jobs
                SET candidate_git_sha = base_git_sha, status='SUCCEEDED'
                WHERE id = ?
                """,
                (job.id,),
            )
            conn.execute(
                """
                INSERT INTO run_events (job_id, event_type, status, message)
                VALUES (?, 'job.succeeded', 'SUCCEEDED', 'historical')
                """,
                (job.id,),
            )
            create_job(
                conn,
                human_id=f"{hid}__ASSURANCE_FUNCTIONAL",
                project_human_id="PRJ-003",
                repository_root=repo,
                agent_role="ASSURANCE_FUNCTIONAL",
                queue="ASSURANCE_FUNCTIONAL",
                status="READY",
            )
            conn.execute(
                """
                UPDATE orchestration_jobs
                SET source_delivery_job_id = ?, source_candidate_sha = base_git_sha
                WHERE human_id = ?
                """,
                (job.id, f"{hid}__ASSURANCE_FUNCTIONAL"),
            )
        add_job_dependency(
            conn,
            get_job_by_human_id(conn, "JOB-P2-INTEGRATION").id,
            get_job_by_human_id(conn, "JOB-P2-DEL-DUE-OVERDUE").id,
        )
        add_job_dependency(
            conn,
            get_job_by_human_id(conn, "JOB-P2-INTEGRATION").id,
            get_job_by_human_id(conn, "JOB-P2-DEL-PRIORITY-FILTER").id,
        )
        add_job_dependency(
            conn,
            get_job_by_human_id(conn, "JOB-P2-RELEASE").id,
            get_job_by_human_id(conn, "JOB-P2-INTEGRATION").id,
        )
        before_events = conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]

    result = reconcile_prj003_iter002_fat(
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        ensure_work_items=False,
        work_item_map={"due_overdue": "US-007", "priority_filter": "US-008"},
    )
    assert result.ok
    with connection(db) as conn:
        after_events = conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
        assert after_events >= before_events
        for hid in ("JOB-P2-DEL-DUE-OVERDUE", "JOB-P2-DEL-PRIORITY-FILTER"):
            job = get_job_by_human_id(conn, hid)
            assert job.status == "SUCCEEDED"
            assert job.outcome == "INVALIDATED"
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM run_events WHERE job_id=? AND "
                    "event_type='job.succeeded'",
                    (job.id,),
                ).fetchone()[0]
                >= 1
            )
        assert get_job_by_human_id(conn, "JOB-P2-DEL-DUE-OVERDUE__REWORK-1")
        assert get_job_by_human_id(conn, "JOB-P2-DEL-PRIORITY-FILTER__REWORK-1")
        integ = get_job_by_human_id(conn, "JOB-P2-INTEGRATION")
        rel = get_job_by_human_id(conn, "JOB-P2-RELEASE")
        assert integ.status == "READY"
        assert rel.status == "READY"
        assert not dependencies_satisfied(conn, integ.id)


def test_evaluate_delivery_helper_no_change() -> None:
    job = OrchestrationJob(
        id=1,
        human_id="J",
        project_human_id="PRJ-003",
        repository_root="/tmp",
        iteration_human_id=None,
        work_item_type="story",
        work_item_human_id="US-1",
        agent_role="DELIVERY",
        queue="DELIVERY",
        status="RUNNING",
        priority=1,
        attempt=0,
        max_attempts=3,
        worktree_name=None,
        worktree_path=None,
        base_git_sha="a",
        candidate_git_sha=None,
        requires_worktree=True,
        identity_snapshot_json=None,
        output_ref=None,
        last_error=None,
        created_at="t",
        ready_at=None,
        started_at=None,
        completed_at=None,
        updated_at="t",
        allows_no_change=False,
    )
    ev = evaluate_delivery_candidate(
        job,
        base_git_sha="a",
        candidate_git_sha="a",
        dirty=False,
        cursor_stdout="OUTCOME: NO_CHANGE\nbecause already present",
    )
    assert ev.ok
    assert ev.outcome == "NO_CHANGE"
    assert not ev.handoff_eligible


def test_fat_cli_help() -> None:
    assert main(["fat", "--help"]) == 0
    assert main(["fat", "reconcile", "--help"]) == 0
