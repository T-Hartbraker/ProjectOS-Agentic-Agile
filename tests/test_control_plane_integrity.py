"""Control-plane integrity regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes.orchestration_fakes import SequencedAssuranceExecutor, make_git_remediation_worker
from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.domain_events import EventContext
from projectos.execution_run import create_execution_run, update_execution_run
from projectos.migrate import initialize_database
from projectos.pm_remediation import run_qa_with_remediation
from projectos.qa_gate import collect_qa_gate_facts
from projectos.qa_handoff import create_assurance_jobs_for_delivery, record_assurance_result
from projectos.qa_manager import execute_qa_manager_aggregation
from projectos.remediation_capability import resolve_remediation_execution
from projectos.remediation_executor import production_remediation_worker
from projectos.remediation_store import create_remediation_work
from projectos.sponsor_handoff import create_sponsor_handoff, mark_handoff_accepted
from projectos.store import create_job, get_job, mark_succeeded
from projectos.worker_status import worker_succeeded


def _ctx(tmp_path: Path) -> tuple[Path, str]:
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    repo_root = str(repo.resolve())
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": repo_root, "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return db, repo_root


def _seed_run(conn) -> EventContext:
    handoff = create_sponsor_handoff(
        conn,
        project_id="PRJ-003",
        team_id="T1",
        channel_id="C1",
        thread_ts="1.0",
        sponsor_user_id="U1",
        request_type="RELEASE",
        objective="ship",
    )
    run = create_execution_run(
        conn,
        project_id="PRJ-003",
        handoff_id=handoff.handoff_id,
        request_type="RELEASE",
        objective="ship",
    )
    mark_handoff_accepted(conn, handoff_id=handoff.handoff_id, run_id=run.run_id)
    update_execution_run(conn, run_id=run.run_id, status="RUNNING")
    return EventContext(project_id="PRJ-003", handoff_id=handoff.handoff_id, run_id=run.run_id)


def test_worker_succeeded_accepts_lowercase_status() -> None:
    assert worker_succeeded("succeeded") is True
    assert worker_succeeded("SUCCEEDED") is True
    assert worker_succeeded("failed") is False


def test_security_finding_executes_via_delivery_queue() -> None:
    owner, assigned, queue, _ = resolve_remediation_execution(
        {
            "category": "SECURITY_FINDING",
            "source_gate_or_review": "ASSURANCE_SECURITY",
        }
    )
    assert owner == "security-agent"
    assert assigned == "developer-agent"
    assert queue == "DELIVERY"


def test_qa_fail_does_not_create_rework_job(tmp_path: Path) -> None:
    db, repo_root = _ctx(tmp_path)
    with connection(db) as conn:
        event_ctx = _seed_run(conn)
        delivery = create_job(
            conn,
            human_id="DEL-1",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
            base_git_sha="shaA",
            run_id=event_ctx.run_id,
        )
        assurance = create_job(
            conn,
            human_id="QA-SEC",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="ASSURANCE_SECURITY",
            queue="ASSURANCE_SECURITY",
            status="SUCCEEDED",
            base_git_sha="shaA",
            run_id=event_ctx.run_id,
        )
        from projectos.store import insert_qa_evidence, set_job_source_provenance

        set_job_source_provenance(
            conn, assurance.id, source_delivery_job_id=delivery.id, source_candidate_sha="shaA"
        )
        insert_qa_evidence(
            conn,
            project_human_id="PRJ-003",
            repository_root=repo_root,
            delivery_job_id=delivery.id,
            assurance_job_id=assurance.id,
            candidate_git_sha="shaA",
            assurance_role="ASSURANCE_SECURITY",
            result="pending",
        )
        assurance = get_job(conn, assurance.id)
        record_assurance_result(conn, assurance, verdict="FAIL", evidence_ref="e", findings=[
            {
                "finding_id": "FND-1",
                "category": "SECURITY_FINDING",
                "severity": "high",
                "evidence": "x",
                "affected_component": "auth",
                "expected_condition": "ok",
                "actual_condition": "bad",
                "recommended_owner_role": "SECURITY_FINDING",
            }
        ])
        rework = conn.execute(
            "SELECT 1 FROM orchestration_jobs WHERE human_id LIKE '%__REWORK'"
        ).fetchone()
    assert rework is None


def test_stale_null_run_qa_does_not_satisfy_current_run(tmp_path: Path) -> None:
    db, repo_root = _ctx(tmp_path)
    with connection(db) as conn:
        event_ctx = _seed_run(conn)
        conn.execute(
            """
            INSERT INTO qa_evidence (
                project_human_id, repository_root, candidate_git_sha,
                assurance_role, result, run_id
            ) VALUES ('PRJ-003', ?, 'sha-old', 'ASSURANCE_FUNCTIONAL', 'pass', NULL)
            """,
            (repo_root,),
        )
        facts = collect_qa_gate_facts(
            conn,
            project_id="PRJ-003",
            candidate_git_sha="sha-new",
            run_id=event_ctx.run_id,
        )
    assert facts["gate"] != "PASSED"


def test_qa_manager_aggregates_assessor_evidence(tmp_path: Path) -> None:
    db, repo_root = _ctx(tmp_path)
    with connection(db) as conn:
        event_ctx = _seed_run(conn)
        delivery = create_job(
            conn,
            human_id="DEL-MGR",
            project_human_id="PRJ-003",
            repository_root=repo_root,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
            base_git_sha="sha-base",
            run_id=event_ctx.run_id,
        )
        conn.execute(
            "UPDATE orchestration_jobs SET candidate_git_sha = 'shaB', base_git_sha = 'sha-base' WHERE id = ?",
            (delivery.id,),
        )
        delivery = get_job(conn, delivery.id)
        handoff = create_assurance_jobs_for_delivery(conn, delivery, candidate_git_sha="shaB")
        for hid in handoff.assurance_job_ids:
            if "QA_MANAGER" in hid:
                continue
            job = conn.execute(
                "SELECT id FROM orchestration_jobs WHERE human_id = ?", (hid,)
            ).fetchone()
            assurance = get_job(conn, int(job["id"]))
            record_assurance_result(conn, assurance, verdict="PASS", evidence_ref="ok")
            conn.execute(
                "UPDATE qa_evidence SET run_id = ? WHERE assurance_job_id = ?",
                (event_ctx.run_id, assurance.id),
            )
        mgr = conn.execute(
            "SELECT id FROM orchestration_jobs WHERE human_id = ?",
            (f"{delivery.human_id}__QA_MANAGER",),
        ).fetchone()
        mgr_job = get_job(conn, int(mgr["id"]))
        mark_succeeded(conn, mgr_job.id, output_ref="mgr", candidate_git_sha="shaB")
        result = execute_qa_manager_aggregation(conn, mgr_job)
        mgr_evidence = conn.execute(
            """
            SELECT result FROM qa_evidence
            WHERE assurance_role = 'QA_MANAGER' AND candidate_git_sha = 'shaB'
            """
        ).fetchone()
    assert result["aggregate_result"] == "pass"
    assert mgr_evidence["result"] == "pass"


def test_closed_loop_with_qa_manager(tmp_path: Path) -> None:
    db, repo_root = _ctx(tmp_path)
    worker = make_git_remediation_worker(repo_root)
    assurance = SequencedAssuranceExecutor([False, True])
    with connection(db) as conn:
        event_ctx = _seed_run(conn)
        for i in range(4):
            conn.execute(
                """
                INSERT INTO qa_evidence (
                    project_human_id, repository_root, candidate_git_sha,
                    assurance_role, result, run_id
                ) VALUES ('PRJ-003', ?, 'shaA', ?, 'fail', ?)
                """,
                (repo_root, f"ASSURANCE_{i}", event_ctx.run_id),
            )
        result = run_qa_with_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            worker=worker,
            assurance_executor=assurance,
            max_cycles=3,
        )
    assert result.gate == "PASSED"
