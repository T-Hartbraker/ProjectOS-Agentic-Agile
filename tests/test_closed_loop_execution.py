"""Closed-loop execution tests — real work, immutable evidence, candidate retest."""

from __future__ import annotations

import json
from pathlib import Path

from fakes.orchestration_fakes import (
    FakeAssuranceExecutor,
    SequencedAssuranceExecutor,
    make_git_remediation_worker,
)
from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.domain_events import EventContext
from projectos.execution_run import create_execution_run, update_execution_run
from projectos.finding_routing import (
    ARCHITECTURE_VIOLATION,
    PACKAGING_DEFECT,
    SECURITY_FINDING,
    SOURCE_CODE_DEFECT,
    route_finding_to_agent,
)
from projectos.migrate import initialize_database
from projectos.pm_remediation import collect_qa_findings, run_qa_with_remediation
from projectos.remediation_store import create_remediation_work
from projectos.services.context import ServiceContext
from projectos.sponsor_handoff import create_sponsor_handoff, mark_handoff_accepted
from projectos.store import get_job_by_human_id


def _ctx(tmp_path: Path) -> tuple[ServiceContext, str]:
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    repo_root = str(repo.resolve())
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": repo_root, "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json"), repo_root


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


def _seed_qa(conn, *, candidate: str, failed: int, total: int, repo_root: str, run_id: str) -> None:
    for i in range(total):
        result = "fail" if i < failed else "pass"
        conn.execute(
            """
            INSERT INTO qa_evidence (
                project_human_id, repository_root, candidate_git_sha,
                assurance_role, result, run_id
            ) VALUES ('PRJ-003', ?, ?, ?, ?, ?)
            """,
            (repo_root, candidate, f"ASSURANCE_{i % 4}", result, run_id),
        )


def test_candidate_a_stays_fail_after_remediation(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)
    worker = make_git_remediation_worker(repo_root)
    assurance = SequencedAssuranceExecutor([True])
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        _seed_qa(conn, candidate="shaA", failed=2, total=4, repo_root=repo_root, run_id=event_ctx.run_id)
        failed_before = {
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM qa_evidence WHERE candidate_git_sha = 'shaA' AND result = 'fail'"
            ).fetchall()
        }
        result = run_qa_with_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            worker=worker,
            assurance_executor=assurance,
        )
        failed_after = {
            int(row["id"]): str(row["result"])
            for row in conn.execute(
                f"""
                SELECT id, result FROM qa_evidence
                WHERE id IN ({",".join("?" * len(failed_before))})
                """,
                tuple(failed_before),
            ).fetchall()
        }
        assert len(assurance.calls) == 1
    assert result.gate == "PASSED"
    assert failed_before
    assert all(result == "fail" for result in failed_after.values())


def test_second_cycle_failure_then_third_passes(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)
    worker = make_git_remediation_worker(repo_root)
    assurance = SequencedAssuranceExecutor([False, True])
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        _seed_qa(conn, candidate="shaA", failed=4, total=4, repo_root=repo_root, run_id=event_ctx.run_id)
        result = run_qa_with_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            worker=worker,
            assurance_executor=assurance,
            max_cycles=3,
        )
        candidates = {
            row["candidate_git_sha"]
            for row in conn.execute(
                "SELECT DISTINCT candidate_git_sha FROM qa_evidence"
            ).fetchall()
        }
    assert result.gate == "PASSED"
    assert "shaA" in candidates
    assert any(c != "shaA" for c in candidates)
    assert len(assurance.calls) == 2


def test_finding_routing_assignments() -> None:
    dev_agent, _ = route_finding_to_agent({"category": SOURCE_CODE_DEFECT})
    arch_agent, _ = route_finding_to_agent({"category": ARCHITECTURE_VIOLATION})
    sec_agent, _ = route_finding_to_agent({"category": SECURITY_FINDING})
    del_agent, _ = route_finding_to_agent({"category": PACKAGING_DEFECT})
    assert dev_agent == "developer-agent"
    assert arch_agent == "architecture-agent"
    assert sec_agent == "security-agent"
    assert del_agent == "delivery-agent"


def test_developer_remediation_uses_delivery_queue(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        work = create_remediation_work(
            conn,
            run_id=event_ctx.run_id or "RUN-1",
            project_id="PRJ-003",
            remediation_cycle=1,
            finding_ids=["FND-1"],
            assigned_agent="developer-agent",
            objective="Fix defect",
            acceptance_criteria="QA passes",
            source_candidate_id=None,
            repository_root=repo_root,
            assignment_reason="test",
        )
        job = get_job_by_human_id(
            conn,
            f"{event_ctx.run_id}__REMEDIATION_1__developer-agent",
        )
    assert job is not None
    assert job.queue == "DELIVERY"
    assert job.agent_role == "DELIVERY"


def test_work_completed_requires_work_item(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)
    worker = make_git_remediation_worker(repo_root)
    assurance = SequencedAssuranceExecutor([True])
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        _seed_qa(conn, candidate="shaA", failed=2, total=2, repo_root=repo_root, run_id=event_ctx.run_id)
        run_qa_with_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            worker=worker,
            assurance_executor=assurance,
        )
        work = conn.execute(
            "SELECT work_item_id, status, target_candidate_id FROM remediation_work"
        ).fetchone()
        completed = conn.execute(
            """
            SELECT evidence_json FROM projectos_events
            WHERE run_id = ? AND event_type = 'WORK_COMPLETED'
            """,
            (event_ctx.run_id,),
        ).fetchone()
    assert work is not None
    assert work["status"] == "COMPLETED"
    assert work["target_candidate_id"]
    evidence = json.loads(completed["evidence_json"])
    assert evidence.get("work_item_id")
