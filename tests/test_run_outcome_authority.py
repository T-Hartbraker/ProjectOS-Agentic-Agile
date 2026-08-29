"""Tests for PM-only terminal outcome authority."""

from __future__ import annotations

from pathlib import Path

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.execution_run import create_execution_run
from projectos.migrate import initialize_database
from projectos.run_state import apply_event_to_run
from projectos.services.context import ServiceContext


def _ctx(tmp_path: Path) -> ServiceContext:
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def test_operational_qa_failure_does_not_terminalize_run(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        run = create_execution_run(
            conn, project_id="PRJ-003", handoff_id=None, request_type="RELEASE", objective="ship"
        )
        apply_event_to_run(
            conn,
            run_id=run.run_id,
            event_type="QA_GATE_FAILED",
            payload={"phase": "QA_GATE", "actor_id": "qa-agent", "progress": 40},
        )
        row = conn.execute(
            "SELECT status, current_phase FROM execution_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert row["status"] == "PLANNING"
    assert row["current_phase"] == "QA_GATE"


def test_only_pm_terminal_event_closes_run_status(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        run = create_execution_run(
            conn, project_id="PRJ-003", handoff_id=None, request_type="RELEASE", objective="ship"
        )
        apply_event_to_run(
            conn,
            run_id=run.run_id,
            event_type="RUN_BLOCKED",
            payload={"phase": "terminal", "actor_id": "pm-agent"},
        )
        row = conn.execute(
            "SELECT status FROM execution_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert row["status"] == "BLOCKED"


def test_terminal_run_not_reopened_by_operational_events(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        run = create_execution_run(
            conn, project_id="PRJ-003", handoff_id=None, request_type="RELEASE", objective="ship"
        )
        apply_event_to_run(
            conn, run_id=run.run_id, event_type="RUN_BLOCKED", payload={"phase": "terminal"}
        )
        apply_event_to_run(
            conn,
            run_id=run.run_id,
            event_type="AGENT_ASSIGNED",
            payload={"phase": "DELIVERY", "actor_id": "delivery-agent"},
        )
        apply_event_to_run(
            conn,
            run_id=run.run_id,
            event_type="QA_GATE_FAILED",
            payload={"phase": "QA_GATE"},
        )
        row = conn.execute(
            "SELECT status, current_phase FROM execution_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert row["status"] == "BLOCKED"
    assert row["current_phase"] == "terminal"


def test_waiting_for_sponsor_is_nonterminal_and_resumable(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        run = create_execution_run(
            conn, project_id="PRJ-003", handoff_id=None, request_type="RELEASE", objective="ship"
        )
        apply_event_to_run(
            conn,
            run_id=run.run_id,
            event_type="WAITING_FOR_SPONSOR",
            payload={"phase": "sponsor_decision"},
        )
        row = conn.execute(
            "SELECT status FROM execution_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
        assert row["status"] == "WAITING_FOR_SPONSOR"
        apply_event_to_run(
            conn,
            run_id=run.run_id,
            event_type="AGENT_ASSIGNED",
            payload={"phase": "RUNNING", "actor_id": "pm-agent"},
        )
        row2 = conn.execute(
            "SELECT status, current_phase FROM execution_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert row2["status"] == "WAITING_FOR_SPONSOR"
    assert row2["current_phase"] == "RUNNING"
