"""QA closed-loop remediation tests with real executable work."""

from __future__ import annotations

import json
from pathlib import Path

from fakes.orchestration_fakes import SequencedAssuranceExecutor, make_git_remediation_worker
from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.domain_events import EventContext
from projectos.execution_run import create_execution_run, update_execution_run
from projectos.migrate import initialize_database
from projectos.pm_remediation import run_qa_with_remediation
from projectos.services.context import ServiceContext
from projectos.sponsor_handoff import create_sponsor_handoff, mark_handoff_accepted


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


def _seed_qa(conn, *, total: int = 4, failed: int = 2, repo_root: str, run_id: str) -> None:
    for i in range(total):
        result = "fail" if i < failed else "pass"
        conn.execute(
            """
            INSERT INTO qa_evidence (
                project_human_id, repository_root, candidate_git_sha,
                assurance_role, result, run_id
            ) VALUES ('PRJ-003', ?, 'shaA', ?, ?, ?)
            """,
            (repo_root, f"ASSURANCE_{i % 2}", result, run_id),
        )


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


def test_qa_fail_remediation_pass_continue(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)
    worker = make_git_remediation_worker(repo_root)
    assurance = SequencedAssuranceExecutor([True])
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        _seed_qa(conn, total=4, failed=2, repo_root=repo_root, run_id=event_ctx.run_id)
        failed_before = {
            int(row["id"])
            for row in conn.execute(
                """
                SELECT id FROM qa_evidence
                WHERE candidate_git_sha = 'shaA' AND result = 'fail'
                """
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
        events = {
            r["event_type"]
            for r in conn.execute(
                "SELECT event_type FROM projectos_events WHERE run_id = ?", (event_ctx.run_id,)
            ).fetchall()
        }
        run = conn.execute(
            "SELECT status FROM execution_runs WHERE run_id = ?", (event_ctx.run_id,)
        ).fetchone()
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
    assert result.gate == "PASSED"
    assert result.remediation_cycles == 1
    assert "REMEDIATION_STARTED" in events
    assert "WORK_COMPLETED" in events
    assert "QA_RETEST_STARTED" in events
    assert "RUN_BLOCKED" not in events
    assert run["status"] == "RUNNING"
    assert failed_before
    assert all(result == "fail" for result in failed_after.values())


def test_remediation_policy_exceeded_escalates(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)
    worker = make_git_remediation_worker(repo_root)
    assurance = SequencedAssuranceExecutor([False, False, False])
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        _seed_qa(conn, total=4, failed=4, repo_root=repo_root, run_id=event_ctx.run_id)
        result = run_qa_with_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            max_cycles=3,
            max_same_finding_recurrence=3,
            worker=worker,
            assurance_executor=assurance,
        )
        terminal = conn.execute(
            "SELECT event_type FROM projectos_events WHERE run_id=? AND event_type='RUN_ESCALATED'",
            (event_ctx.run_id,),
        ).fetchone()
        run = conn.execute(
            "SELECT status FROM execution_runs WHERE run_id=?", (event_ctx.run_id,)
        ).fetchone()
    assert result.escalated
    assert terminal is not None
    assert run["status"] == "ESCALATED"


def test_qa_failure_alone_never_run_blocked(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        _seed_qa(conn, total=2, failed=2, repo_root=repo_root, run_id=event_ctx.run_id)
        run_qa_with_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            max_cycles=0,
        )
        blocked = conn.execute(
            "SELECT 1 FROM projectos_events WHERE run_id = ? AND event_type = 'RUN_BLOCKED'",
            (event_ctx.run_id,),
        ).fetchone()
    assert blocked is None
