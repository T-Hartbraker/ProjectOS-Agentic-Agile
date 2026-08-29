"""Orchestration backbone tests — mutation ingress, events, crash boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.domain_events import EventContext, emit_projectos_event, ACTOR_PM
from projectos.event_dispatcher import dispatch_event_outbox
from projectos.migrate import initialize_database
from projectos.pm_capabilities import ensure_pm_run_for_approved_proposal, proposal_to_handoff
from projectos.services.context import ServiceContext
from projectos.slack_chatgpt import execute_projectos_proposal, _optional_fresh_projectos_facts
from projectos.sponsor_query import SponsorQueryService
from projectos.chatgpt_proposals import create_proposal
from projectos.store import add_slack_interface_channel


def _ctx(tmp_path: Path) -> ServiceContext:
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    with connection(db) as conn:
        add_slack_interface_channel(conn, channel_id="C1", team_id="T1", is_default=True)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def _count_table(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_read_only_query_does_not_mutate(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    initialize_database(ctx.db_path)
    with connection(ctx.db_path) as conn:
        before = {
            "handoffs": _count_table(conn, "sponsor_handoffs"),
            "runs": _count_table(conn, "execution_runs"),
            "events": _count_table(conn, "projectos_events"),
            "releases": _count_table(conn, "delivery_releases"),
        }
    facts = _optional_fresh_projectos_facts(ctx, project_id="PRJ-003", cleaned="What is the project status?")
    assert facts
    with connection(ctx.db_path) as conn:
        after = {
            "handoffs": _count_table(conn, "sponsor_handoffs"),
            "runs": _count_table(conn, "execution_runs"),
            "events": _count_table(conn, "projectos_events"),
            "releases": _count_table(conn, "delivery_releases"),
        }
    assert before == after


def test_execute_projectos_proposal_rejects_mutations(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(Exception, match="SponsorHandoff"):
        execute_projectos_proposal(
            ctx,
            {"project_id": "PRJ-003", "intent": "prepare_release", "instruction": "{}"},
        )


def test_crash_before_commit_no_mutation_no_event(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    initialize_database(ctx.db_path)
    with connection(ctx.db_path) as conn:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO sponsor_handoffs (handoff_id, project_id, team_id, channel_id, thread_ts, "
            "sponsor_user_id, request_type, objective, status) "
            "VALUES ('HND-TEST', 'PRJ-003', 'T1', 'C1', '1.0', 'U1', 'WORK', 'test', 'DRAFT')"
        )
        conn.execute("ROLLBACK")
    with connection(ctx.db_path) as conn2:
        assert _count_table(conn2, "sponsor_handoffs") == 0
        assert _count_table(conn2, "projectos_events") == 0


def test_authoritative_mutation_and_event_atomic(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    initialize_database(ctx.db_path)
    with connection(ctx.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sponsor_handoffs (
                handoff_id, project_id, team_id, channel_id, thread_ts,
                sponsor_user_id, request_type, objective, status
            ) VALUES ('HND-1', 'PRJ-003', 'T1', 'C1', '1.0', 'U1', 'RELEASE', 'ship', 'ACCEPTED_BY_PM')
            """
        )
        conn.execute(
            """
            INSERT INTO execution_runs (
                run_id, project_id, handoff_id, request_type, objective, status
            ) VALUES ('RUN-1', 'PRJ-003', 'HND-1', 'RELEASE', 'ship', 'PLANNING')
            """
        )
        event_ctx = EventContext(
            project_id="PRJ-003",
            handoff_id="HND-1",
            run_id="RUN-1",
            slack_team_id="T1",
            slack_channel_id="C1",
            slack_thread_ts="1.0",
        )
        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="HANDOFF_ACCEPTED",
            summary="ship",
            actor_id=ACTOR_PM,
            detail_level="milestone",
        )
        conn.commit()
    with connection(ctx.db_path) as conn:
        assert _count_table(conn, "projectos_events") == 1
        assert _count_table(conn, "event_outbox") == 1


def test_crash_before_slack_projection_retries_once(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    initialize_database(ctx.db_path)
    posts: list[dict] = []

    def fake_post(**kwargs):
        posts.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr("projectos.event_dispatcher.post_message", fake_post)
    with connection(ctx.db_path) as conn:
        emit_projectos_event(
            conn,
            ctx=EventContext(
                project_id="PRJ-003",
                run_id="RUN-1",
                slack_channel_id="C1",
                slack_thread_ts="1.0",
            ),
            event_type="WORK_STARTED",
            summary="started",
            actor_id=ACTOR_PM,
        )
    # simulate crash before dispatch — outbox still pending
    with connection(ctx.db_path) as conn:
        assert conn.execute(
            "SELECT status FROM event_outbox LIMIT 1"
        ).fetchone()["status"] == "pending"
    stats = dispatch_event_outbox(ctx.db_path)
    assert stats["delivered"] == 1
    stats2 = dispatch_event_outbox(ctx.db_path)
    assert stats2["delivered"] == 0
    assert len(posts) == 1


def test_slack_api_failure_does_not_lose_event(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    initialize_database(ctx.db_path)
    calls = {"n": 0}

    def flaky_post(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("slack down")
        return {"ok": True}

    monkeypatch.setattr("projectos.event_dispatcher.post_message", flaky_post)
    with connection(ctx.db_path) as conn:
        emit_projectos_event(
            conn,
            ctx=EventContext(project_id="PRJ-003", slack_channel_id="C1", slack_thread_ts="1.0"),
            event_type="QA_GATE_PASSED",
            summary="QA passed",
            actor_id=ACTOR_PM,
            evidence={"tests_total": 10, "tests_passed": 10, "gate": "PASSED"},
        )
    first = dispatch_event_outbox(ctx.db_path)
    assert first["failed"] == 1
    second = dispatch_event_outbox(ctx.db_path)
    assert second["delivered"] == 1


def test_duplicate_projector_delivery_idempotent(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    initialize_database(ctx.db_path)
    posts: list[str] = []

    def fake_post(**kwargs):
        posts.append(str(kwargs.get("text")))
        return {"ok": True}

    monkeypatch.setattr("projectos.event_dispatcher.post_message", fake_post)
    with connection(ctx.db_path) as conn:
        emit_projectos_event(
            conn,
            ctx=EventContext(project_id="PRJ-003", slack_channel_id="C1", slack_thread_ts="1.0"),
            event_type="PACKAGE_COMPLETED",
            summary="packaged",
            actor_id=ACTOR_PM,
        )
    dispatch_event_outbox(ctx.db_path)
    dispatch_event_outbox(ctx.db_path)
    assert len(posts) == 1


def test_worker_failure_event_visible(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    initialize_database(ctx.db_path)
    from projectos.cockpit_worker import emit_worker_terminal_event

    job = MagicMock()
    job.human_id = "JOB-001"
    job.project_human_id = "PRJ-003"
    job.queue = "ASSURANCE_FUNCTIONAL"
    job.agent_role = "ASSURANCE_FUNCTIONAL"
    job.work_item_human_id = None
    with connection(ctx.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sponsor_handoffs (
                handoff_id, project_id, team_id, channel_id, thread_ts,
                sponsor_user_id, request_type, objective, status, run_id
            ) VALUES ('HND-1', 'PRJ-003', 'T1', 'C1', '1.0', 'U1', 'RELEASE', 'ship', 'ACCEPTED_BY_PM', 'RUN-1')
            """
        )
        conn.execute(
            """
            INSERT INTO execution_runs (
                run_id, project_id, handoff_id, request_type, objective, status
            ) VALUES ('RUN-1', 'PRJ-003', 'HND-1', 'RELEASE', 'ship', 'RUNNING')
            """
        )
        emit_worker_terminal_event(conn, job, status="FAILED", error="tests failed")
    with connection(ctx.db_path) as conn:
        row = conn.execute(
            "SELECT event_type, evidence_json FROM projectos_events WHERE event_type = 'WORK_FAILED'"
        ).fetchone()
        assert row is not None
        evidence = json.loads(row["evidence_json"])
        assert evidence.get("error_category") == "failed"


def test_qa_typed_facts_in_query_service(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _, facts = SponsorQueryService(ctx).get_quality_summary("PRJ-003")
    assert "gate_status" in facts
    assert "assurance_reviews_completed" in facts
    assert "semantic_rules" in facts


def test_legacy_proposal_converted_to_pm_run(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    initialize_database(ctx.db_path)
    with connection(ctx.db_path) as conn:
        proposal = create_proposal(
            conn,
            team_id="T1",
            channel_id="C1",
            thread_ts="1.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            intent="work_request",
            instruction="Add telemetry dashboard",
        )
        run_id, event_ctx = ensure_pm_run_for_approved_proposal(ctx, conn, proposal=proposal)
        assert run_id
        assert event_ctx.run_id == run_id
        handoff = proposal_to_handoff(proposal)
        assert handoff.objective == "Add telemetry dashboard"
        events = conn.execute(
            "SELECT event_type FROM projectos_events WHERE event_type = 'HANDOFF_ACCEPTED'"
        ).fetchall()
        assert len(events) == 1


def test_legacy_slack_outbox_receives_no_new_rows(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    from projectos.agent_activity import enqueue_slack_activity

    with connection(ctx.db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM slack_activity_outbox").fetchone()[0]
        result = enqueue_slack_activity(
            conn,
            team_id="T1",
            channel_id="C1",
            thread_ts="1.0",
            idempotency_key="legacy:test",
            payload={"text": "should not enqueue"},
        )
        after = conn.execute("SELECT COUNT(*) FROM slack_activity_outbox").fetchone()[0]
    assert result == 0
    assert int(before) == int(after)


def test_qa_gate_atomic_with_outbox(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    initialize_database(ctx.db_path)
    with connection(ctx.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sponsor_handoffs (
                handoff_id, project_id, team_id, channel_id, thread_ts,
                sponsor_user_id, request_type, objective, status, run_id
            ) VALUES ('HND-QA', 'PRJ-003', 'T1', 'C1', '1.0', 'U1', 'RELEASE', 'ship', 'ACCEPTED_BY_PM', 'RUN-QA')
            """
        )
        conn.execute(
            """
            INSERT INTO execution_runs (
                run_id, project_id, handoff_id, request_type, objective, status
            ) VALUES ('RUN-QA', 'PRJ-003', 'HND-QA', 'RELEASE', 'ship', 'RUNNING')
            """
        )
        from projectos.qa_gate import emit_qa_gate_evaluation

        emit_qa_gate_evaluation(
            conn,
            project_id="PRJ-003",
            event_context=EventContext(
                project_id="PRJ-003",
                handoff_id="HND-QA",
                run_id="RUN-QA",
                slack_channel_id="C1",
                slack_thread_ts="1.0",
            ),
        )
        events = conn.execute(
            "SELECT event_type FROM projectos_events WHERE run_id = 'RUN-QA'"
        ).fetchall()
        outbox = conn.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0]
    types = {r["event_type"] for r in events}
    assert "QA_STARTED" in types
    assert "QA_GATE_PASSED" in types
    assert int(outbox) >= 2


def test_stub_installer_blocks_run_completion(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        "projectos.pm_agent.orchestrate_release_capability",
        None,
    )
    from projectos.run_evidence import close_execution_run

    with connection(ctx.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sponsor_handoffs (
                handoff_id, project_id, team_id, channel_id, thread_ts,
                sponsor_user_id, request_type, objective, status, run_id,
                desired_outputs_json
            ) VALUES (
                'HND-STUB', 'PRJ-003', 'T1', 'C1', '1.0', 'U1', 'RELEASE',
                'give me the finished installer', 'ACCEPTED_BY_PM', 'RUN-STUB',
                '{"installer": true, "download_link": true}'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO execution_runs (
                run_id, project_id, handoff_id, request_type, objective, status
            ) VALUES (
                'RUN-STUB', 'PRJ-003', 'HND-STUB', 'RELEASE',
                'give me the finished installer', 'RUNNING'
            )
            """
        )
        close_execution_run(
            conn,
            event_ctx=EventContext(
                project_id="PRJ-003",
                handoff_id="HND-STUB",
                run_id="RUN-STUB",
            ),
            terminal_status="BLOCKED",
            summary="Requested finished installer cannot be supplied.",
            failure={
                "phase": "INSTALLER",
                "reason": "stub installer",
                "retryable": False,
            },
        )
        row = conn.execute(
            "SELECT status FROM execution_runs WHERE run_id = 'RUN-STUB'"
        ).fetchone()
    assert row["status"] == "BLOCKED"


def test_release_published_triggers_pm_run_completion(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sponsor_handoffs (
                handoff_id, project_id, team_id, channel_id, thread_ts,
                sponsor_user_id, request_type, objective, status, run_id
            ) VALUES ('HND-1', 'PRJ-003', 'T1', 'C1', '1.0', 'U1', 'RELEASE', 'ship', 'ACCEPTED_BY_PM', 'RUN-PUB')
            """
        )
        conn.execute(
            """
            INSERT INTO execution_runs (
                run_id, project_id, handoff_id, request_type, objective, status
            ) VALUES ('RUN-PUB', 'PRJ-003', 'HND-1', 'RELEASE', 'ship', 'RUNNING')
            """
        )
        emit_projectos_event(
            conn,
            ctx=EventContext(project_id="PRJ-003", run_id="RUN-PUB"),
            event_type="RELEASE_PUBLISHED",
            summary="published",
            actor_id=ACTOR_PM,
        )
        row = conn.execute(
            "SELECT status FROM execution_runs WHERE run_id = 'RUN-PUB'"
        ).fetchone()
    assert row["status"] == "COMPLETED"


def test_terminal_evidence_is_authoritative(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    from projectos.run_evidence import build_terminal_evidence, close_execution_run

    with connection(ctx.db_path) as conn:
        conn.execute(
            """
            INSERT INTO sponsor_handoffs (
                handoff_id, project_id, team_id, channel_id, thread_ts,
                sponsor_user_id, request_type, objective, status, run_id
            ) VALUES ('HND-EV', 'PRJ-003', 'T1', 'C1', '1.0', 'U1', 'WORK', 'add docs', 'ACCEPTED_BY_PM', 'RUN-EV')
            """
        )
        conn.execute(
            """
            INSERT INTO execution_runs (
                run_id, project_id, handoff_id, request_type, objective, status
            ) VALUES ('RUN-EV', 'PRJ-003', 'HND-EV', 'WORK', 'add docs', 'RUNNING')
            """
        )
        close_execution_run(
            conn,
            event_ctx=EventContext(project_id="PRJ-003", run_id="RUN-EV"),
            terminal_status="COMPLETED",
            summary="done",
        )
        evidence = build_terminal_evidence(conn, run_id="RUN-EV")
    assert evidence["run_id"] == "RUN-EV"
    assert evidence["terminal_status"] == "COMPLETED"
    assert evidence["objective"] == "add docs"
