"""Application-service façades: callable without CLI, CLI stays compatible."""

from __future__ import annotations

from pathlib import Path

import pytest

from projectos.cli import main
from projectos.db import connection
from projectos.errors import OrchestrationError, RegistryError
from projectos.migrate import initialize_database
from projectos.services import (
    ApprovalService,
    DaemonService,
    DispatchService,
    LearningService,
    PlanService,
    RecoverService,
    RegistryService,
    ReportingService,
    ServiceContext,
    StatusService,
    WorkerService,
)
from projectos.store import create_job, insert_agent_run
from helpers import fake_status, init_git_repo, write_identity, write_registry


def _ctx(tmp_path: Path, repo: Path, project_id: str = "PRJ-003") -> ServiceContext:
    write_identity(repo, project_human_id=project_id)
    config = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": project_id,
                "repository_root": str(repo.resolve()),
                "enabled": True,
            }
        ],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return ServiceContext(db_path=db, registry_path=config)


def test_registry_onboarding_via_service(tmp_path: Path, monkeypatch) -> None:
    repo = init_git_repo(tmp_path / "repo")
    ctx = _ctx(tmp_path, repo)
    svc = RegistryService(ctx)
    listed = svc.list_projects()
    assert listed[0].project_human_id == "PRJ-003"
    shown = svc.show("PRJ-003")
    assert shown.repository_root == repo.resolve()
    with pytest.raises(RegistryError, match="not in the registry"):
        svc.show("PRJ-404")

    from projectos import validation as validation_mod

    monkeypatch.setattr(
        validation_mod,
        "run_projectctl_status",
        lambda root: fake_status("PRJ-003"),
    )
    report = svc.validate()
    assert report.ok
    assert report.validated[0].entry.project_human_id == "PRJ-003"


def test_registry_cli_still_lists(tmp_path: Path, capsys) -> None:
    repo = init_git_repo(tmp_path / "repo")
    ctx = _ctx(tmp_path, repo)
    assert main(["--config", str(ctx.registry_path), "registry", "list"]) == 0
    assert "PRJ-003" in capsys.readouterr().out


def test_recover_requires_job_id_in_service_and_cli(
    tmp_path: Path, capsys
) -> None:
    repo = init_git_repo(tmp_path / "repo")
    ctx = _ctx(tmp_path, repo)
    svc = RecoverService(ctx)
    with pytest.raises(OrchestrationError, match="--salvage-candidate requires --job"):
        svc.salvage(None)
    with pytest.raises(OrchestrationError, match="--reclaim-running requires --job"):
        svc.reclaim_running("")

    code = main(
        [
            "--config",
            str(ctx.registry_path),
            "recover",
            "--db",
            str(ctx.db_path),
            "--salvage-candidate",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "--salvage-candidate requires --job" in err


def test_plan_dispatch_worker_and_status_services(tmp_path: Path, monkeypatch) -> None:
    repo = init_git_repo(tmp_path / "repo")
    ctx = _ctx(tmp_path, repo)
    from projectos import validation as validation_mod

    monkeypatch.setattr(
        validation_mod,
        "run_projectctl_status",
        lambda root: fake_status("PRJ-003"),
    )
    plan = {
        "schema_version": 1,
        "project_human_id": "PRJ-003",
        "sponsor_authority": "approved",
        "jobs": [
            {
                "human_id": "JOB-P1",
                "queue": "PM",
                "agent_role": "PM",
            }
        ],
    }
    result = PlanService(ctx).run(
        "PRJ-003",
        dry_run=True,
        projectctl_runner=lambda root: fake_status("PRJ-003"),
        plan_override=plan,
    )
    assert result.ok
    assert result.status == "dry_run"

    idle = WorkerService(ctx).run_once(skip_identity_validation=True)
    assert idle.status in {"idle", "skipped", "IDLE"} or idle.job_human_id in (None, "")

    dispatched = DispatchService(ctx).run(
        once=True, skip_identity_validation=True, max_parallel=1
    )
    assert dispatched.mode in {"once", "until_idle"} or dispatched.message

    status = StatusService(ctx)
    assert status.jobs_for_project("PRJ-003") == []
    with pytest.raises(OrchestrationError, match="not found"):
        status.job("JOB-MISSING")
    daemon = DaemonService(ctx).status()
    assert daemon.status in {"stopped", "running", "idle"} or daemon.pid is None


def test_learning_and_reporting_inputs(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    ctx = _ctx(tmp_path, repo)
    with connection(ctx.db_path) as conn:
        job = create_job(
            conn,
            human_id="JOB-L1",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
            identity_snapshot={"project_human_id": "PRJ-003"},
        )
        insert_agent_run(
            conn,
            job_id=job.id,
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
            usage={"input_tokens": 1, "output_tokens": 2},
            error=None,
        )
    jobs = LearningService(ctx).job_history("PRJ-003")
    assert [j.job_human_id for j in jobs] == ["JOB-L1"]
    runs = LearningService(ctx).agent_runs("PRJ-003")
    assert len(runs) == 1
    assert runs[0].exit_code == 0

    budget = ReportingService(ctx).budget("PRJ-003")
    assert budget.project_human_id == "PRJ-003"
    doctor = ReportingService(ctx).doctor()
    assert doctor.findings
    ReportingService(ctx).upsert_schedule("PRJ-003", cadence="daily")
    schedules = ReportingService(ctx).list_schedules()
    assert schedules[0].project_human_id == "PRJ-003"


def test_approvals_sponsor_authority(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    ctx = _ctx(tmp_path, repo)
    svc = ApprovalService(ctx)
    good = {
        "schema_version": 1,
        "project_human_id": "PRJ-003",
        "sponsor_authority": "approved",
        "jobs": [{"human_id": "JOB-P1", "queue": "PM", "agent_role": "PM"}],
    }
    bad = dict(good, sponsor_authority="unapproved")
    assert svc.sponsor_granted(good, expected_project_id="PRJ-003")
    assert not svc.sponsor_granted(bad, expected_project_id="PRJ-003")
    errors = svc.plan_errors(bad, expected_project_id="PRJ-003")
    assert any("Sponsor-authority" in e for e in errors)


def test_fat_reconcile_rejects_other_projects(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    ctx = _ctx(tmp_path, repo)
    with pytest.raises(OrchestrationError, match="only PRJ-003 / ITER-002"):
        RecoverService(ctx).reconcile_fat("PRJ-001", "ITER-001")
    assert (
        main(
            [
                "--config",
                str(ctx.registry_path),
                "fat",
                "reconcile",
                "--db",
                str(ctx.db_path),
                "--project",
                "PRJ-001",
                "--iteration",
                "ITER-001",
            ]
        )
        == 1
    )
