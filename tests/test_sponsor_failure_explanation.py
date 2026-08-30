"""Sponsor failure explanation queries grounded in authoritative evidence."""

from __future__ import annotations

from pathlib import Path

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.domain_events import event_context_from_thread
from projectos.execution_run import create_execution_run
from projectos.migrate import initialize_database
from projectos.operational_failure import record_operational_failure
from projectos.services.context import ServiceContext
from projectos.slack_chatgpt import handle_chatgpt_slack_message
from projectos.sponsor_handoff import create_sponsor_handoff, mark_handoff_accepted
from projectos.sponsor_query import SponsorQueryService
from projectos.store import add_slack_interface_channel

FAT_MESSAGE = (
    "Start a new project to build a simple Python command-line calculator. It must "
    "support addition, subtraction, multiplication, and division, include automated "
    "tests, and be packaged as a distributable ZIP. Use the full ProjectOS delivery "
    "process. Proceed autonomously through implementation, QA, remediation if "
    "required, packaging, and release. Ask me only if a Sponsor decision is genuinely "
    "required."
)

TEAM = "T1"
CHANNEL = "G_PRIVATE"
THREAD = "500.0"
SPONSOR = "U1"
CHATGPT_USER = "UCHATGPT"


def _ctx(tmp_path: Path) -> ServiceContext:
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-004", project_name="Calculator")
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-004", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    with connection(db) as conn:
        add_slack_interface_channel(conn, channel_id=CHANNEL, team_id=TEAM, is_default=True)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def test_failure_explanation_uses_authoritative_event(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        handoff = create_sponsor_handoff(
            conn,
            project_id="PRJ-004",
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
            sponsor_user_id=SPONSOR,
            request_type="WORK",
            objective=FAT_MESSAGE,
        )
        run = create_execution_run(
            conn,
            project_id="PRJ-004",
            handoff_id=handoff.handoff_id,
            request_type="WORK",
            objective=FAT_MESSAGE,
        )
        mark_handoff_accepted(conn, handoff_id=handoff.handoff_id, run_id=run.run_id)
        thread = event_context_from_thread(
            project_id="PRJ-004",
            handoff_id=handoff.handoff_id,
            run_id=run.run_id,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
        )
        record_operational_failure(
            conn,
            event_ctx=thread,
            component="pm_work_execution",
            operation="intake_submit",
            error_category="missing_work_item_reference",
            error_detail="unknown work item story US-001",
            recoverable=True,
            phase="intake",
            work_item_type="story",
            work_item_human_id="US-001",
        )

    explanation = SponsorQueryService(ctx).get_failure_explanation(
        "PRJ-004",
        thread_key=THREAD,
    )
    assert "PRJ-004" in explanation
    assert run.run_id in explanation
    assert "US-001" in explanation
    assert "intake" in explanation.casefold()
    assert "cannot invent" not in explanation.casefold()


def test_failure_explanation_without_evidence_does_not_hallucinate(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    explanation = SponsorQueryService(ctx).get_failure_explanation("PRJ-004")
    assert "does not contain authoritative failure evidence" in explanation


def test_slack_why_did_it_fail_returns_grounded_explanation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PROJECTOS_SLACK_CHATGPT_USER_ID", CHATGPT_USER)
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _ctx(tmp_path)

    with connection(ctx.db_path) as conn:
        from projectos.chatgpt_store import upsert_chatgpt_thread

        upsert_chatgpt_thread(
            conn,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
            sponsor_user_id=SPONSOR,
            project_human_id="PRJ-004",
            active=True,
        )
        handoff = create_sponsor_handoff(
            conn,
            project_id="PRJ-004",
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
            sponsor_user_id=SPONSOR,
            request_type="WORK",
            objective=FAT_MESSAGE,
        )
        run = create_execution_run(
            conn,
            project_id="PRJ-004",
            handoff_id=handoff.handoff_id,
            request_type="WORK",
            objective=FAT_MESSAGE,
        )
        mark_handoff_accepted(conn, handoff_id=handoff.handoff_id, run_id=run.run_id)
        thread = event_context_from_thread(
            project_id="PRJ-004",
            handoff_id=handoff.handoff_id,
            run_id=run.run_id,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
        )
        record_operational_failure(
            conn,
            event_ctx=thread,
            component="pm_work_execution",
            operation="intake_submit",
            error_category="missing_work_item_reference",
            error_detail="unknown work item story US-001",
            recoverable=True,
            phase="intake",
            work_item_type="story",
            work_item_human_id="US-001",
        )

    reply = handle_chatgpt_slack_message(
        ctx,
        text=f"<@{CHATGPT_USER}> Why did it fail?",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD,
        message_ts="500.1",
        user_id=SPONSOR,
    )
    assert reply is not None
    body = str(reply.get("text") or "")
    assert "US-001" in body
    assert run.run_id in body
