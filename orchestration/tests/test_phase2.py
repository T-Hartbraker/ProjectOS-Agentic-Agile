"""Phase 2 consolidation tests: plan, dispatch, QA, schedule, daemon, doctor, budget."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from projectos.budget import build_budget_report
from projectos.clock import FakeClock
from projectos.cli import main
from projectos.daemon import DaemonLock, get_daemon_status, run_daemon
from projectos.db import connection
from projectos.dispatch import run_dispatch
from projectos.doctor import run_doctor
from projectos.errors import OrchestrationError
from projectos.integration import integrate_candidates
from projectos.iteration import run_iteration
from projectos.plan import run_plan, validate_plan_document
from projectos.projectctl_bridge import ProjectctlStatusResult
from projectos.qa_handoff import (
    create_assurance_jobs_for_delivery,
    record_assurance_result,
)
from projectos.schedule import evaluate_due, upsert_schedule
from projectos.store import (
    add_job_dependency,
    create_job,
    get_job,
    get_job_by_human_id,
    insert_agent_run,
    mark_succeeded,
)
from projectos.worker import run_once

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


def test_all_top_level_help() -> None:
    for cmd in (
        None,
        "registry",
        "plan",
        "worker",
        "dispatch",
        "recover",
        "budget",
        "iteration",
        "schedule",
        "daemon",
        "doctor",
        "fat",
        "cursor",
    ):
        argv = ["--help"] if cmd is None else [cmd, "--help"]
        assert main(argv) == 0


def test_plan_schema_validation_and_reject() -> None:
    errors = validate_plan_document(
        {
            "schema_version": 1,
            "project_human_id": "PRJ-003",
            "sponsor_authority": "approved",
            "jobs": [
                {
                    "human_id": "J1",
                    "queue": "DELIVERY",
                    "agent_role": "DELIVERY",
                    "depends_on": ["J1"],
                }
            ],
        },
        expected_project_id="PRJ-003",
    )
    assert any("cycle" in e for e in errors)

    errors2 = validate_plan_document(
        {
            "schema_version": 1,
            "project_human_id": "PRJ-003",
            "sponsor_authority": "nope",
            "jobs": [
                {"human_id": "J1", "queue": "NOT_A_QUEUE", "agent_role": "X"}
            ],
        },
        expected_project_id="PRJ-003",
    )
    assert any("Sponsor" in e for e in errors2)
    assert any("queue" in e for e in errors2)


def test_plan_accepts_and_dry_run(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    plan = {
        "schema_version": 1,
        "project_human_id": "PRJ-003",
        "sponsor_authority": "approved",
        "jobs": [
            {
                "human_id": "JOB-P1",
                "queue": "DELIVERY",
                "agent_role": "DELIVERY",
                "work_item_type": "story",
                "work_item_human_id": "STORY-1",
                "depends_on": [],
                "priority": 10,
            }
        ],
    }
    dry = run_plan(
        project_human_id="PRJ-003",
        dry_run=True,
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        plan_override=plan,
    )
    assert dry.status == "dry_run"
    assert dry.plan is not None
    assert dry.plan["jobs"]
    with connection(db) as conn:
        assert get_job_by_human_id(conn, "JOB-P1") is None
        before = conn.execute("SELECT COUNT(*) FROM orchestration_jobs").fetchone()[0]

    accepted = run_plan(
        project_human_id="PRJ-003",
        dry_run=False,
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        plan_override=plan,
    )
    assert accepted.status == "accepted"
    assert "JOB-P1" in accepted.jobs_created

    # Dry-run after accepted plan: non-empty proposal, zero new jobs, no mutation.
    with connection(db) as conn:
        mid = conn.execute("SELECT COUNT(*) FROM orchestration_jobs").fetchone()[0]
        assert mid == before + 1
        job_row = conn.execute(
            "SELECT status, queue FROM orchestration_jobs WHERE human_id='JOB-P1'"
        ).fetchone()
        assert job_row["status"] == "READY"

    dry2 = run_plan(
        project_human_id="PRJ-003",
        dry_run=True,
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        plan_override=plan,
    )
    assert dry2.status == "dry_run"
    assert dry2.plan is not None
    assert len(dry2.plan["jobs"]) == 1
    assert dry2.jobs_created == []
    with connection(db) as conn:
        after = conn.execute("SELECT COUNT(*) FROM orchestration_jobs").fetchone()[0]
        assert after == mid
        job_row2 = conn.execute(
            "SELECT status, queue FROM orchestration_jobs WHERE human_id='JOB-P1'"
        ).fetchone()
        assert job_row2["status"] == "READY"
        assert job_row2["queue"] == "DELIVERY"


def test_plan_dry_run_replays_accepted_when_cursor_empty(tmp_path: Path) -> None:
    from projectos.plan import extract_json_document

    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    plan = {
        "schema_version": 1,
        "project_human_id": "PRJ-003",
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
        ],
    }
    accepted = run_plan(
        project_human_id="PRJ-003",
        dry_run=False,
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        plan_override=plan,
    )
    assert accepted.status == "accepted"
    with connection(db) as conn:
        count_before = conn.execute("SELECT COUNT(*) FROM orchestration_jobs").fetchone()[
            0
        ]

    empty_runner = make_cursor_runner(returncode=0, stdout="", stderr="")
    dry = run_plan(
        project_human_id="PRJ-003",
        dry_run=True,
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        cursor_runner=empty_runner,
    )
    assert dry.status == "dry_run"
    assert dry.plan_source == "accepted_replay"
    assert dry.plan is not None
    assert len(dry.plan["jobs"]) == 2
    assert dry.jobs_created == []
    with connection(db) as conn:
        count_after = conn.execute("SELECT COUNT(*) FROM orchestration_jobs").fetchone()[
            0
        ]
        assert count_after == count_before
        for hid in ("JOB-P2-PM-SETUP", "JOB-P2-ARCH"):
            assert get_job_by_human_id(conn, hid) is not None

    with pytest.raises(OrchestrationError, match="empty"):
        extract_json_document("")

    # Empty cursor with no accepted plan still errors.
    db2 = seed_db(tmp_path / "projectos2.db")
    err = run_plan(
        project_human_id="PRJ-003",
        dry_run=True,
        db_path=db2,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        cursor_runner=empty_runner,
    )
    assert err.status == "error"
    assert "empty" in (err.error or "").lower()


def test_plan_extracts_cursor_json_envelope() -> None:
    from projectos.plan import extract_json_document

    envelope = json.dumps(
        {
            "type": "result",
            "result": json.dumps(
                {
                    "schema_version": 1,
                    "project_human_id": "PRJ-003",
                    "sponsor_authority": "approved",
                    "jobs": [
                        {
                            "human_id": "J1",
                            "queue": "PM",
                            "agent_role": "PM",
                            "depends_on": [],
                        }
                    ],
                }
            ),
        }
    )
    plan = extract_json_document(envelope)
    assert plan["jobs"][0]["human_id"] == "J1"


def test_dispatch_independent_overlap_and_dependency_order(tmp_path: Path) -> None:
    repo_a = init_git_repo(tmp_path / "repo-a")
    repo_b = init_git_repo(tmp_path / "repo-b")
    _write_identity(repo_a, "PRJ-A")
    _write_identity(repo_b, "PRJ-B")
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
    db = seed_db(tmp_path / "projectos.db")
    started: dict[str, float] = {}
    finished: dict[str, float] = {}
    lock = threading.Lock()
    release_b = threading.Event()

    def slow_runner(cmd, **kwargs):
        prompt = cmd[-1] if cmd else ""
        job = "A" if "JOB-A" in prompt else ("B" if "JOB-B" in prompt else "C")
        with lock:
            started[job] = time.perf_counter()
        if job == "A":
            # Hold A long enough that B can start overlapping.
            time.sleep(0.2)
        elif job == "B":
            time.sleep(0.05)
        elif job == "C":
            # C must not start until A and B finished (deps).
            assert "A" in finished and "B" in finished
        with lock:
            finished[job] = time.perf_counter()
        return FakeCompletedProcess(0, f"ok-{job}", "")

    with connection(db) as conn:
        a = create_job(
            conn,
            human_id="JOB-A",
            project_human_id="PRJ-A",
            repository_root=repo_a,
            agent_role="PM",
            queue="PM",
            status="READY",
        )
        b = create_job(
            conn,
            human_id="JOB-B",
            project_human_id="PRJ-B",
            repository_root=repo_b,
            agent_role="PM",
            queue="PM",
            status="READY",
        )
        c = create_job(
            conn,
            human_id="JOB-C",
            project_human_id="PRJ-A",
            repository_root=repo_a,
            agent_role="PM",
            queue="PM",
            status="READY",
        )
        add_job_dependency(conn, c.id, a.id)
        add_job_dependency(conn, c.id, b.id)

    result = run_dispatch(
        until_idle=True,
        max_parallel=3,
        db_path=db,
        registry_path=cfg,
        cursor_runner=slow_runner,
        skip_identity_validation=True,
        max_waves=20,
    )
    assert result.exit_code == 0
    assert "A" in started and "B" in started
    # Overlap: each started before the other finished
    assert started["A"] < finished["B"] and started["B"] < finished["A"]
    assert finished["C"] > finished["A"] and finished["C"] > finished["B"]
    with connection(db) as conn:
        assert get_job_by_human_id(conn, "JOB-C").status == "SUCCEEDED"


def test_qa_pass_fail_rework_and_stale_evidence(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        delivery = create_job(
            conn,
            human_id="JOB-D1",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="RUNNING",
            requires_worktree=True,
        )
        mark_succeeded(conn, delivery.id, output_ref=None, candidate_git_sha="sha-old")
        conn.execute(
            "UPDATE orchestration_jobs SET base_git_sha = ? WHERE id = ?",
            ("sha-base", delivery.id),
        )
        delivery = get_job(conn, delivery.id)
        handoff = create_assurance_jobs_for_delivery(
            conn, delivery, candidate_git_sha="sha-old"
        )
        assert len(handoff.assurance_job_ids) >= 4

        # Pass one assurance job
        func = get_job_by_human_id(conn, "JOB-D1__ASSURANCE_FUNCTIONAL")
        assert func is not None
        mark_succeeded(conn, func.id, output_ref="e1", candidate_git_sha="sha-old")
        func = get_job(conn, func.id)
        record_assurance_result(conn, func, passed=True, evidence_ref="e1")
        row = conn.execute(
            "SELECT result FROM qa_evidence WHERE assurance_job_id = ?",
            (func.id,),
        ).fetchone()
        assert row["result"] == "pass"

        # Fail -> rework
        sec = get_job_by_human_id(conn, "JOB-D1__ASSURANCE_SECURITY")
        mark_succeeded(conn, sec.id, output_ref="e2", candidate_git_sha="sha-old")
        sec = get_job(conn, sec.id)

        class DefectOut:
            stdout = "Created BUG-001"

        record_assurance_result(
            conn,
            sec,
            passed=False,
            evidence_ref="e2",
            create_defect_fn=lambda *a, **k: DefectOut(),
        )
        rework = get_job_by_human_id(conn, "JOB-D1__ASSURANCE_SECURITY__REWORK")
        assert rework is not None
        assert rework.queue == "DELIVERY"

        # Stale evidence: delivery moved to new SHA
        conn.execute(
            "UPDATE orchestration_jobs SET candidate_git_sha = ? WHERE id = ?",
            ("sha-new", delivery.id),
        )
        integ = get_job_by_human_id(conn, "JOB-D1__ASSURANCE_INTEGRATION")
        mark_succeeded(conn, integ.id, output_ref="e3", candidate_git_sha="sha-old")
        integ = get_job(conn, integ.id)
        with pytest.raises(OrchestrationError, match="Stale QA"):
            record_assurance_result(conn, integ, passed=True, evidence_ref="e3")
        stale = conn.execute(
            "SELECT result FROM qa_evidence WHERE assurance_job_id = ?",
            (integ.id,),
        ).fetchone()
        assert stale["result"] == "stale_rejected"


def test_integration_clean_and_conflict(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    # Create two divergent commits on branches
    import subprocess

    def git(args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git(["checkout", "-b", "feat-a"])
    (repo / "a.txt").write_text("a", encoding="utf-8")
    git(["add", "a.txt"])
    git(["commit", "-m", "a"])
    sha_a = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    git(["checkout", "master"])
    # master may be main on some git - detect
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
    ).strip()
    git(["checkout", "-b", "feat-b"])
    (repo / "b.txt").write_text("b", encoding="utf-8")
    git(["add", "b.txt"])
    git(["commit", "-m", "b"])
    sha_b = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    git(["checkout", branch])

    db = seed_db(tmp_path / "projectos.db")
    result = integrate_candidates(
        repository_root=repo,
        project_human_id="PRJ-003",
        source_shas=[sha_a, sha_b],
        source_job_ids=[1, 2],
        db_path=db,
    )
    assert result.status == "succeeded"
    assert result.integrated_sha

    # Conflict case: same file different content
    git(["checkout", "-b", "c1"])
    (repo / "conflict.txt").write_text("one", encoding="utf-8")
    git(["add", "conflict.txt"])
    git(["commit", "-m", "c1"])
    sha_c1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    git(["checkout", branch])
    git(["checkout", "-b", "c2"])
    (repo / "conflict.txt").write_text("two", encoding="utf-8")
    git(["add", "conflict.txt"])
    git(["commit", "-m", "c2"])
    sha_c2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    git(["checkout", branch])

    conflicted = integrate_candidates(
        repository_root=repo,
        project_human_id="PRJ-003",
        source_shas=[sha_c1, sha_c2],
        source_job_ids=[3, 4],
        integration_branch="projectos/integration-conflict",
        db_path=db,
    )
    assert conflicted.status == "conflict"
    assert conflicted.conflict_paths


def test_schedule_idempotent_with_fake_clock(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        upsert_schedule(
            conn,
            project_human_id="PRJ-003",
            enabled=True,
            timezone="UTC",
            cadence="daily",
            local_time="09:00",
        )
    clock = FakeClock(datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc))
    r1 = evaluate_due(
        db_path=db,
        registry_path=cfg,
        clock=clock.now,
        projectctl_runner=_fake_status(),
    )
    assert any(d.triggered for d in r1.due)
    r2 = evaluate_due(
        db_path=db,
        registry_path=cfg,
        clock=clock.now,
        projectctl_runner=_fake_status(),
    )
    assert all(not d.triggered for d in r2.due)
    assert any("already triggered" in d.reason for d in r2.due)


def test_daemon_single_instance_and_loops(tmp_path: Path) -> None:
    import os

    db = seed_db(tmp_path / "projectos.db")
    lock_path = tmp_path / "daemon.lock"
    lock = DaemonLock(lock_path)
    lock.acquire()
    with pytest.raises(OrchestrationError, match="already running"):
        DaemonLock(lock_path).acquire()
    lock.release()

    slept: list[float] = []
    # Use empty registry file that loads but has no projects
    cfg = write_registry(tmp_path / "projects.json", [])
    code = run_daemon(
        db_path=db,
        registry_path=cfg,
        poll_seconds=0.01,
        max_loops=2,
        sleep_fn=lambda s: slept.append(s),
        skip_identity_validation=True,
        lock_path=tmp_path / "daemon-run.lock",
    )
    assert code == 0
    assert len(slept) >= 1
    status = get_daemon_status(db)
    assert status.status == "stopped"


def test_budget_no_token_fabrication(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-BGT",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
        )
        insert_agent_run(
            conn,
            job_id=job.id,
            worker_id="w",
            cursor_command=["agent"],
            prompt_ref=None,
            output_ref=None,
            stdout_ref=None,
            stderr_ref=None,
            exit_code=0,
            started_at="t0",
            ended_at="t1",
            duration_ms=100,
            worktree_name=None,
            worktree_path=None,
            base_git_sha=None,
            candidate_git_sha=None,
            dirty=None,
            usage={"status": "unknown"},
            error=None,
        )
        insert_agent_run(
            conn,
            job_id=job.id,
            worker_id="w",
            cursor_command=["agent"],
            prompt_ref=None,
            output_ref=None,
            stdout_ref=None,
            stderr_ref=None,
            exit_code=0,
            started_at="t0",
            ended_at="t1",
            duration_ms=50,
            worktree_name=None,
            worktree_path=None,
            base_git_sha=None,
            candidate_git_sha=None,
            dirty=None,
            usage={"input_tokens": 10, "output_tokens": 5},
            error=None,
        )
    report = build_budget_report(project_human_id="PRJ-003", db_path=db)
    assert report.cursor_invocations == 2
    assert report.unreported_usage_count == 1
    assert report.token_input == 10
    assert report.token_output == 5


def test_doctor_blocking_exit(tmp_path: Path) -> None:
    db = seed_db(tmp_path / "projectos.db")
    # Missing registry path -> blocking
    report = run_doctor(db_path=db, registry_path=tmp_path / "nope.json")
    assert report.blocking
    assert report.exit_code == 1


def test_iteration_restart_idempotent(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    plan = {
        "schema_version": 1,
        "project_human_id": "PRJ-003",
        "sponsor_authority": "approved",
        "jobs": [
            {
                "human_id": "JOB-IT1",
                "queue": "PM",
                "agent_role": "PM",
                "depends_on": [],
            }
        ],
    }
    r1 = run_iteration(
        project_human_id="PRJ-003",
        iteration_human_id="ITER-1",
        dry_run=False,
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        cursor_runner=make_cursor_runner(),
        plan_override=plan,
        skip_identity_validation=True,
    )
    assert r1.exit_code == 0
    r2 = run_iteration(
        project_human_id="PRJ-003",
        iteration_human_id="ITER-1",
        dry_run=False,
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        cursor_runner=make_cursor_runner(),
        plan_override=plan,
        skip_identity_validation=True,
    )
    # Restart should skip completed checkpoints (idempotent progress)
    assert "recovery" in r2.checkpoints or r2.status in {
        "RELEASE_READY",
        "QUALITY_HOLD",
        "RUNNING",
        "READY",
    }
