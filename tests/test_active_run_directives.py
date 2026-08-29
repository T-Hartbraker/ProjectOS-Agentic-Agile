"""Active-run Sponsor directive routing tests."""

from __future__ import annotations

from pathlib import Path

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.execution_run import create_execution_run, update_execution_run
from projectos.migrate import initialize_database
from projectos.pm_agent import accept_sponsor_handoff
from projectos.services.context import ServiceContext
from projectos.slack_advisor_handoff import HandoffRequest
from projectos.sponsor_handoff import create_sponsor_handoff, mark_handoff_accepted

TEAM = "T1"
CHANNEL = "C1"
THREAD = "1.0"
SPONSOR = "U1"


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


def _seed_active_release_run(conn) -> str:
    handoff = create_sponsor_handoff(
        conn,
        project_id="PRJ-003",
        team_id=TEAM,
        channel_id=CHANNEL,
        thread_ts=THREAD,
        sponsor_user_id=SPONSOR,
        request_type="RELEASE",
        objective="Re-release package with installer",
    )
    run = create_execution_run(
        conn,
        project_id="PRJ-003",
        handoff_id=handoff.handoff_id,
        request_type="RELEASE",
        objective=handoff.objective,
    )
    mark_handoff_accepted(conn, handoff_id=handoff.handoff_id, run_id=run.run_id)
    update_execution_run(conn, run_id=run.run_id, status="RUNNING")
    return run.run_id


def test_sponsor_directive_routes_to_same_run(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        "projectos.pm_agent.orchestrate_release_capability",
        lambda *args, **kwargs: "mock evidence",
    )
    with connection(ctx.db_path) as conn:
        run_id = _seed_active_release_run(conn)
        follow_up = HandoffRequest(
            project_id="PRJ-003",
            objective="Investigate the context error and resolve it.",
            action_type="work_request",
            rationale="",
            scope="",
            constraints="{}",
            acceptance_intent="",
            exclusions="",
            source_conversation_summary="",
        )
        result = accept_sponsor_handoff(
            ctx,
            conn,
            handoff=follow_up,
            project_id="PRJ-003",
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
            sponsor_user_id=SPONSOR,
        )
        run_count = conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
        events = {
            row["event_type"]
            for row in conn.execute(
                "SELECT event_type FROM projectos_events WHERE run_id = ?", (run_id,)
            ).fetchall()
        }
    assert result.run_id == run_id
    assert int(run_count) == 1
    assert "SPONSOR_DIRECTIVE_RECEIVED" in events
    assert "PLAN_UPDATED" in events
    assert "PM_REPLAN" in events


def test_separate_objective_creates_new_run(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        "projectos.pm_agent.orchestrate_release_capability",
        lambda *args, **kwargs: "mock evidence",
    )
    with connection(ctx.db_path) as conn:
        _seed_active_release_run(conn)
        separate = HandoffRequest(
            project_id="PRJ-003",
            objective="Brand new objective: build a separate analytics dashboard.",
            action_type="work_request",
            rationale="",
            scope="",
            constraints="{}",
            acceptance_intent="",
            exclusions="",
            source_conversation_summary="",
        )
        result = accept_sponsor_handoff(
            ctx,
            conn,
            handoff=separate,
            project_id="PRJ-003",
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
            sponsor_user_id=SPONSOR,
        )
        run_count = conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
    assert int(run_count) == 2
    assert result.run_id.startswith("RUN-")
    assert "HANDOFF_ACCEPTED" in result.projectos_text or result.handoff_id.startswith("HND-")
