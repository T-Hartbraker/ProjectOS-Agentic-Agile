"""Restart recovery integration tests for control-plane closure."""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from control_plane_helpers import create_release_handoff, delivery_json, git_parent, setup_release_project
from fakes.orchestration_fakes import make_git_remediation_worker
from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.delivery.contract import delivery_contract_missing_evidence
from projectos.domain_events import EventContext
from projectos.execution_run import update_execution_run
from projectos.migrate import initialize_database
from projectos.qa_inconclusive import schedule_assurance_retry_for_inconclusive
from projectos.qa_handoff import create_assurance_jobs_for_delivery, record_assurance_result
from projectos.recover import run_recovery
from projectos.release_failure_actions import (
    ensure_package_failure_next_action,
    ensure_publication_failure_next_action,
)
from projectos.release_preparation_actions import ensure_release_preparation_next_action
from projectos.remediation_store import create_remediation_work
from projectos.run_liveness import assert_nonterminal_run_has_durable_next_action
from projectos.run_next_actions import list_active_next_actions
from projectos.services.context import ServiceContext
from projectos.store import create_job, get_job, promote_retry_wait_to_ready, utc_now


def _fresh_ctx(db_path: Path, registry_path: Path) -> ServiceContext:
    return ServiceContext(db_path=db_path, registry_path=registry_path)


def test_startup_remediation_recovery_executes_real_work(tmp_path: Path, monkeypatch) -> None:
    ctx, repo, _ = setup_release_project(tmp_path, github_release_enabled=False)
    repo_root = str(repo)
    worker = make_git_remediation_worker(repo_root)

    from projectos.recover import IdentityCheckResult

    monkeypatch.setattr(
        "projectos.recover._check_project_identity",
        lambda project_human_id, **kwargs: IdentityCheckResult(
            project_human_id=project_human_id,
            ok=True,
            repository_root=repo_root,
        ),
    )

    def _fake_production_worker(
        conn,
        *,
        work,
        event_ctx,
        project_id,
        repository_root,
        service_ctx,
    ):
        return worker(
            conn,
            work=work,
            event_ctx=event_ctx,
            project_id=project_id,
            repository_root=repository_root,
        )

    monkeypatch.setattr(
        "projectos.remediation_executor.production_remediation_worker",
        _fake_production_worker,
    )
    with connection(ctx.db_path) as conn:
        _, run, event_ctx = create_release_handoff(conn, project_id="PRJ-004")
        work = create_remediation_work(
            conn,
            run_id=run.run_id,
            project_id="PRJ-004",
            remediation_cycle=1,
            finding_ids=["FND-1"],
            assigned_agent="developer-agent",
            objective="fix defect",
            acceptance_criteria="candidate produced",
            source_candidate_id=None,
            repository_root=repo_root,
            assignment_reason="restart test",
        )
        assert_nonterminal_run_has_durable_next_action(conn, run_id=run.run_id, project_id="PRJ-004")

    db_path = ctx.db_path
    registry_path = ctx.registry_path
    del ctx
    fresh = _fresh_ctx(db_path, registry_path)
    report = run_recovery(db_path=fresh.db_path, registry_path=fresh.registry_path, service_ctx=fresh)
    with connection(fresh.db_path) as conn:
        completed = conn.execute(
            """
            SELECT COUNT(*) FROM projectos_events
            WHERE run_id = ? AND event_type = 'WORK_COMPLETED'
            """,
            (run.run_id,),
        ).fetchone()[0]
        unavailable = conn.execute(
            """
            SELECT COUNT(*) FROM projectos_events
            WHERE run_id = ? AND event_type = 'WORK_EXECUTION_UNAVAILABLE'
            """,
            (run.run_id,),
        ).fetchone()[0]
    assert int(unavailable) == 0
    assert any("Resumed" in msg for msg in report.messages)
    assert int(completed) == 1


def test_delivery_contract_recovery_schedules_release_prep_retry(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "product")
    write_identity(repo, project_human_id="PRJ-004", project_name="Example Product")
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/example-product.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-004", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    ctx = ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")
    failure = delivery_contract_missing_evidence(repo)
    with connection(db) as conn:
        _, run, event_ctx = create_release_handoff(conn, project_id="PRJ-004")
        ensure_release_preparation_next_action(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-004",
            repository_root=str(repo),
            failure=failure,
        )
        unbacked = conn.execute(
            """
            SELECT 1 FROM run_next_actions
            WHERE run_id = ? AND action_type = 'REMEDIATION_WORK'
              AND remediation_work_id IS NULL
            """,
            (run.run_id,),
        ).fetchone()
        active = list_active_next_actions(conn, run_id=run.run_id)
        retry = conn.execute(
            "SELECT status FROM orchestration_jobs WHERE human_id LIKE '%RELEASE_PREP_RETRY'"
        ).fetchone()
    assert unbacked is None
    assert retry is not None
    assert active


@pytest.mark.parametrize(
    "failure_kind,failure,action_fn",
    [
        (
            "QA_INCONCLUSIVE",
            None,
            "inconclusive",
        ),
        (
            "PACKAGE_FAILED",
            {"blocker_type": "PACKAGE_FAILED", "reason": "build failed", "retryable": True},
            "package",
        ),
        (
            "PUBLICATION_FAILED",
            {"blocker_type": "PUBLICATION_FAILED", "reason": "github down", "retryable": True},
            "publication",
        ),
        (
            "RELEASE_PREPARATION_BLOCKED",
            {"blocker_type": "RELEASE_PREPARATION_BLOCKED", "reason": "blocked", "retryable": True},
            "release_prep",
        ),
    ],
)
def test_failure_restart_recovery_survives(tmp_path: Path, failure_kind: str, failure: dict | None, action_fn: str) -> None:
    ctx, repo, candidate_a = setup_release_project(tmp_path, github_release_enabled=False)
    repo_root = str(repo)
    with connection(ctx.db_path) as conn:
        _, run, event_ctx = create_release_handoff(conn, project_id="PRJ-004")
        if action_fn == "inconclusive":
            delivery = create_job(
                conn,
                human_id=f"DEL-{run.run_id[-8:]}",
                project_human_id="PRJ-004",
                repository_root=repo_root,
                agent_role="DELIVERY",
                queue="DELIVERY",
                status="SUCCEEDED",
                base_git_sha=git_parent(Path(repo_root)),
                run_id=run.run_id,
            )
            conn.execute(
                "UPDATE orchestration_jobs SET candidate_git_sha = ? WHERE id = ?",
                (candidate_a, delivery.id),
            )
            delivery = get_job(conn, delivery.id)
            handoff = create_assurance_jobs_for_delivery(conn, delivery, candidate_git_sha=candidate_a)
            for hid in handoff.assurance_job_ids:
                if "QA_MANAGER" in hid:
                    continue
                row = conn.execute("SELECT id, queue FROM orchestration_jobs WHERE human_id = ?", (hid,)).fetchone()
                job = get_job(conn, int(row["id"]))
                verdict = "INCONCLUSIVE" if row["queue"] == "ASSURANCE_SECURITY" else "PASS"
                record_assurance_result(conn, job, verdict=verdict, evidence_ref="e")
                conn.execute(
                    "UPDATE qa_evidence SET run_id = ? WHERE assurance_job_id = ?",
                    (run.run_id, job.id),
                )
            schedule_assurance_retry_for_inconclusive(
                conn,
                event_ctx=event_ctx,
                project_id="PRJ-004",
                repository_root=repo_root,
                candidate_git_sha=candidate_a,
                run_id=run.run_id,
                inconclusive_roles=["ASSURANCE_SECURITY"],
            )
        elif action_fn == "package":
            ensure_package_failure_next_action(
                conn,
                event_ctx=event_ctx,
                project_id="PRJ-004",
                repository_root=repo_root,
                failure=failure or {},
            )
        elif action_fn == "publication":
            ensure_publication_failure_next_action(
                conn,
                event_ctx=event_ctx,
                project_id="PRJ-004",
                repository_root=repo_root,
                failure=failure or {},
                release_record_id="DLV-TEST",
            )
        else:
            (Path(repo) / "project" / "delivery.json").write_text(
                json.dumps(delivery_json(github_release_enabled=False)),
                encoding="utf-8",
            )
            ensure_release_preparation_next_action(
                conn,
                event_ctx=event_ctx,
                project_id="PRJ-004",
                repository_root=repo_root,
                failure=failure or {},
            )
        assert_nonterminal_run_has_durable_next_action(conn, run_id=run.run_id, project_id="PRJ-004")
        before = list_active_next_actions(conn, run_id=run.run_id)

    db_path = ctx.db_path
    registry_path = ctx.registry_path
    del ctx
    fresh = _fresh_ctx(db_path, registry_path)
    report = run_recovery(db_path=fresh.db_path, registry_path=fresh.registry_path, service_ctx=fresh)
    with connection(fresh.db_path) as conn:
        after = list_active_next_actions(conn, run_id=run.run_id)
        if action_fn == "publication":
            job_row = conn.execute(
                "SELECT status FROM orchestration_jobs WHERE human_id LIKE '%PUBLICATION_RETRY%'"
            ).fetchone()
            assert job_row is not None
    assert before
    assert after or report.messages
