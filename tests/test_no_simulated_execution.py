"""Prove production orchestration cannot simulate worker or QA success."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fakes.orchestration_fakes import (
    SequencedAssuranceExecutor,
    git_head_sha,
    make_git_remediation_worker,
)
from helpers import init_git_repo, write_identity, write_registry
from projectos.candidate_model import validate_candidate_identity
from projectos.db import connection
from projectos.domain_events import EventContext
from projectos.errors import OrchestrationError
from projectos.execution_run import create_execution_run, update_execution_run
from projectos.migrate import initialize_database
from projectos.pm_remediation import run_qa_with_remediation
from projectos.remediation_executor import RemediationExecutionResult, execute_remediation_work
from projectos.remediation_recovery import resume_outstanding_remediation
from projectos.remediation_store import create_remediation_work
from projectos.services.context import ServiceContext
from projectos.slack_chatgpt import _build_authoritative_context
from projectos.sponsor_handoff import create_sponsor_handoff, mark_handoff_accepted
import pytest


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


def _seed_qa_fail(conn, *, repo_root: str, run_id: str) -> None:
    conn.execute(
        """
        INSERT INTO qa_evidence (
            project_human_id, repository_root, candidate_git_sha,
            assurance_role, result, run_id
        ) VALUES ('PRJ-003', ?, 'shaA', 'ASSURANCE_FUNCTIONAL', 'fail', ?)
        """,
        (repo_root, run_id),
    )


def test_worker_unavailable_emits_execution_unavailable(tmp_path: Path) -> None:
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
            objective="fix",
            acceptance_criteria="pass",
            source_candidate_id="shaA",
            repository_root=repo_root,
            assignment_reason="test",
        )
        outcome = execute_remediation_work(
            conn,
            work=work,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
        )
        events = {
            row["event_type"]
            for row in conn.execute(
                "SELECT event_type FROM projectos_events WHERE run_id = ?", (event_ctx.run_id,)
            ).fetchall()
        }
    assert outcome.status == "UNAVAILABLE"
    assert "WORK_EXECUTION_UNAVAILABLE" in events
    assert "WORK_COMPLETED" not in events
    assert "QA_RETEST_STARTED" not in events


def test_worker_success_without_candidate_rejected(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)

    def bad_worker(conn, *, work, event_ctx, project_id, repository_root):
        return RemediationExecutionResult(
            work_item_id=work.work_item_id,
            status="COMPLETED",
            target_candidate_id=None,
            evidence={},
        )

    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        work = create_remediation_work(
            conn,
            run_id=event_ctx.run_id or "RUN-1",
            project_id="PRJ-003",
            remediation_cycle=1,
            finding_ids=["FND-1"],
            assigned_agent="developer-agent",
            objective="fix",
            acceptance_criteria="pass",
            source_candidate_id=None,
            repository_root=repo_root,
            assignment_reason="test",
        )
        outcome = execute_remediation_work(
            conn,
            work=work,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            worker=bad_worker,
        )
        completed = conn.execute(
            "SELECT 1 FROM projectos_events WHERE run_id = ? AND event_type = 'WORK_COMPLETED'",
            (event_ctx.run_id,),
        ).fetchone()
    assert outcome.status == "FAILED"
    assert completed is None


def test_synthetic_git_candidate_rejected(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)
    with pytest.raises(OrchestrationError, match="synthetic IDs are prohibited"):
        validate_candidate_identity(
            "abc123-remediation-001",
            candidate_type="git_sha",
            repository_root=repo_root,
        )


def test_qa_executor_unavailable_no_gate_passed(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)
    worker = make_git_remediation_worker(repo_root)
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        _seed_qa_fail(conn, repo_root=repo_root, run_id=event_ctx.run_id)
        result = run_qa_with_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            worker=worker,
            assurance_executor=None,
            max_cycles=1,
        )
        passed = conn.execute(
            "SELECT 1 FROM projectos_events WHERE run_id = ? AND event_type = 'QA_GATE_PASSED'",
            (event_ctx.run_id,),
        ).fetchone()
    assert result.gate != "PASSED"
    assert passed is None


def test_assurance_executor_controls_pass_fail(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)
    worker = make_git_remediation_worker(repo_root)
    assurance = SequencedAssuranceExecutor([True])
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        _seed_qa_fail(conn, repo_root=repo_root, run_id=event_ctx.run_id)
        result = run_qa_with_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            worker=worker,
            assurance_executor=assurance,
        )
        assert len(assurance.calls) == 1
        passed = conn.execute(
            "SELECT 1 FROM projectos_events WHERE run_id = ? AND event_type = 'QA_GATE_PASSED'",
            (event_ctx.run_id,),
        ).fetchone()
    assert result.gate == "PASSED"
    assert passed is not None


def test_assurance_executor_exception_surfaces_without_fake_pass(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)
    worker = make_git_remediation_worker(repo_root)

    class BrokenAssurance:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("assurance backend down")

    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        _seed_qa_fail(conn, repo_root=repo_root, run_id=event_ctx.run_id)
        with pytest.raises(RuntimeError, match="assurance backend down"):
            run_qa_with_remediation(
                conn,
                event_ctx=event_ctx,
                project_id="PRJ-003",
                repository_root=repo_root,
                worker=worker,
                assurance_executor=BrokenAssurance(),
                max_cycles=1,
            )
        passed = conn.execute(
            "SELECT 1 FROM projectos_events WHERE run_id = ? AND event_type = 'QA_GATE_PASSED'",
            (event_ctx.run_id,),
        ).fetchone()
    assert passed is None


def test_remediation_recovery_resumes_without_duplicate_completion(tmp_path: Path) -> None:
    ctx, repo_root = _ctx(tmp_path)
    worker = make_git_remediation_worker(repo_root)
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)
        work = create_remediation_work(
            conn,
            run_id=event_ctx.run_id or "RUN-1",
            project_id="PRJ-003",
            remediation_cycle=1,
            finding_ids=["FND-1"],
            assigned_agent="developer-agent",
            objective="fix",
            acceptance_criteria="pass",
            source_candidate_id=git_head_sha(repo_root),
            repository_root=repo_root,
            assignment_reason="test",
        )
        recovery = resume_outstanding_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            worker=worker,
        )
        completed_count = conn.execute(
            """
            SELECT COUNT(*) FROM projectos_events
            WHERE run_id = ? AND event_type = 'WORK_COMPLETED'
            """,
            (event_ctx.run_id,),
        ).fetchone()[0]
        recovery_again = resume_outstanding_remediation(
            conn,
            event_ctx=event_ctx,
            project_id="PRJ-003",
            repository_root=repo_root,
            worker=worker,
        )
        completed_after = conn.execute(
            """
            SELECT COUNT(*) FROM projectos_events
            WHERE run_id = ? AND event_type = 'WORK_COMPLETED'
            """,
            (event_ctx.run_id,),
        ).fetchone()[0]
    assert recovery.resumed == 1
    assert completed_count == 1
    assert recovery_again.resumed == 0
    assert completed_after == 1


def test_dict_enabled_attribute_error_routes_internal_defect(tmp_path: Path) -> None:
    ctx, _repo_root = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        event_ctx = _seed_run(conn)

        class BadSummary:
            pass

        with patch("projectos.slack_sponsor_context.ProjectQueryService") as mock_service:
            instance = mock_service.return_value
            instance.summary.return_value = BadSummary()
            instance.current.return_value = SimpleNamespace(iteration_human_id="ITER-1")
            instance.jobs.return_value = []
            instance.quality.return_value = {}
            instance.releases.return_value = {}
            with pytest.raises(OrchestrationError, match="Internal defect"):
                _build_authoritative_context(
                    ctx,
                    conn,
                    project_id="PRJ-003",
                    team_id="T1",
                    channel_id="C1",
                    thread_key="1.0",
                    sponsor_user_id="U1",
                )
        defect = conn.execute(
            """
            SELECT 1 FROM projectos_events
            WHERE run_id = ? AND event_type = 'INTERNAL_DEFECT_DETECTED'
            """,
            (event_ctx.run_id,),
        ).fetchone()
        evidence = conn.execute(
            """
            SELECT evidence_json FROM projectos_events
            WHERE run_id = ? AND event_type = 'INTERNAL_DEFECT_DETECTED'
            """,
            (event_ctx.run_id,),
        ).fetchone()
    assert defect is not None
    payload = json.loads(evidence["evidence_json"])
    assert payload["error_type"] == "AttributeError"
    assert "enabled" in payload["message"]
