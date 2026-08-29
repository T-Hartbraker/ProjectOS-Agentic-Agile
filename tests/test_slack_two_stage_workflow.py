"""Regression tests for two-stage governed Sponsor workflow."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.chatgpt_proposals import (
    create_proposal,
    get_latest_thread_proposal,
    list_thread_proposals,
    save_proposal_preview,
)
from projectos.chatgpt_store import get_chatgpt_thread, upsert_chatgpt_thread
from projectos.db import connection
from projectos.intake import IntakeResult
from projectos.migrate import initialize_database
from projectos.services.context import ServiceContext
from projectos.slack_chatgpt import handle_chatgpt_slack_message
from projectos.store import add_slack_interface_channel

CHATGPT_USER = "UCHATGPT"
CHANNEL = "C0BSYCCDRST"
TEAM = "T1"
SPONSOR = "U1"
THREAD = "1787963711.959149"
INSTRUCTION = (
    "Propose a harmless governed documentation task for PRJ-003 summarizing "
    "iteration ITER-002 progress and quality review results."
)
RICH_PREVIEW = (
    "*ProjectOS — PREVIEW*\n"
    "_No changes applied. This is a read-only preview._\n"
    "*Proposal:* `prop-1`\n"
    "*Expected orchestration jobs*\n"
    "- JOB-900 / PM / PM"
)
EXECUTION = (
    "*ProjectOS — EXECUTED*\n"
    "*Jobs created*\n"
    "- `JOB-900`"
)


def _mention(text: str) -> str:
    return f"<@{CHATGPT_USER}|ChatGPT> {text}".strip()


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
        add_slack_interface_channel(conn, channel_id=CHANNEL, team_id=TEAM, is_default=True)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def _fake_openai_response(text: str, response_id: str = "resp_seq") -> dict:
    return {
        "id": response_id,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


def _preview_result() -> IntakeResult:
    return IntakeResult(
        status="preview",
        project_human_id="PRJ-003",
        dry_run=True,
        expected_jobs=[{"human_id": "JOB-900", "queue": "PM", "agent_role": "PM"}],
        plan={"iteration_human_id": "ITER-002", "jobs": [{"human_id": "JOB-900", "queue": "PM"}]},
        plan_source="cursor",
    )


def _execution_result() -> IntakeResult:
    return IntakeResult(
        status="submitted",
        project_human_id="PRJ-003",
        dry_run=False,
        jobs_created=["JOB-900"],
        expected_jobs=[{"human_id": "JOB-900", "queue": "PM", "agent_role": "PM"}],
        plan={"iteration_human_id": "ITER-002"},
        plan_source="cursor",
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PROJECTOS_SLACK_CHATGPT_USER_ID", CHATGPT_USER)


def test_two_stage_workflow_preview_then_execute(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    openai_calls = 0
    preview_calls = 0
    submit_calls = 0

    def fake_post(url, headers, body=None):
        nonlocal openai_calls
        if body is not None and isinstance(body, dict) and "input" in body:
            openai_calls += 1
            if openai_calls == 1:
                return _fake_openai_response(
                    '```projectos_handoff\n{"objective":"'
                    + INSTRUCTION.replace('"', "'")
                    + '","action_type":"work_request"}\n```',
                    "resp-1",
                )
            if openai_calls == 2:
                return _fake_openai_response(
                    "Recommend reviewing the persisted preview and approving when ready.",
                    "resp-2",
                )
            return _fake_openai_response("ProjectOS created JOB-900.", f"resp-{openai_calls}")
        return {"ok": True}

    class FakeIntake:
        def preview(self, project_human_id, **kwargs):
            nonlocal preview_calls
            preview_calls += 1
            return _preview_result()

        def submit(self, project_human_id, **kwargs):
            nonlocal submit_calls
            submit_calls += 1
            return _execution_result()

    monkeypatch.setattr("projectos.slack_chatgpt.IntakeService", lambda ctx: FakeIntake())

    propose_reply = handle_chatgpt_slack_message(
        ctx,
        text=_mention("Have ProjectOS do that. " + INSTRUCTION),
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD,
        message_ts="1.0",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    assert propose_reply is not None
    assert "PREVIEW" in propose_reply["text"]
    assert "No changes applied" in propose_reply["text"]
    assert preview_calls == 1
    assert submit_calls == 0
    assert propose_reply.get("blocks")

    with connection(ctx.db_path) as conn:
        proposal = get_latest_thread_proposal(
            conn, team_id=TEAM, channel_id=CHANNEL, thread_ts=THREAD, sponsor_user_id=SPONSOR
        )
        assert proposal is not None
        assert proposal.status == "pending"
        assert proposal.action_type == "work_request"
        assert proposal.preview_result
        assert proposal.result_text is None

    detail_reply = handle_chatgpt_slack_message(
        ctx,
        text="Show me more detail.",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD,
        message_ts="2.0",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    assert detail_reply is not None
    assert "PREVIEW" in detail_reply["text"]
    assert "Update Iteration" not in detail_reply["text"]
    assert openai_calls == 1
    assert preview_calls == 1

    execute_reply = handle_chatgpt_slack_message(
        ctx,
        text="Approved. Execute it.",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD,
        message_ts="3.0",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    assert execute_reply is not None
    assert "EXECUTED" in execute_reply["text"]
    assert "JOB-900" in execute_reply["text"]
    assert submit_calls == 1
    assert preview_calls == 1
    assert openai_calls == 2

    with connection(ctx.db_path) as conn:
        proposal = get_latest_thread_proposal(
            conn, team_id=TEAM, channel_id=CHANNEL, thread_ts=THREAD, sponsor_user_id=SPONSOR
        )
        assert proposal is not None
        assert proposal.status == "completed"
        assert proposal.result_text
        assert "EXECUTED" in proposal.result_text

    replay = handle_chatgpt_slack_message(
        ctx,
        text="Approved. Execute it.",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD,
        message_ts="4.0",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    assert replay is not None
    assert "no pending" in replay["text"].lower()
    assert submit_calls == 1
    assert openai_calls == 2

    evidence = handle_chatgpt_slack_message(
        ctx,
        text="What did you actually change?",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD,
        message_ts="5.0",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    assert evidence is not None
    assert "JOB-900" in evidence["text"]
    assert "EXECUTED" in evidence["text"]
    assert openai_calls == 2


def test_execute_it_does_not_consume_proposal_with_preview_only(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        upsert_chatgpt_thread(
            conn,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
            sponsor_user_id=SPONSOR,
            project_human_id="PRJ-003",
            active=True,
        )
        record = create_proposal(
            conn,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
            sponsor_user_id=SPONSOR,
            project_human_id="PRJ-003",
            intent="work_request",
            instruction=INSTRUCTION,
        )
        save_proposal_preview(
            conn,
            proposal_id=record.proposal_id,
            preview_result=RICH_PREVIEW,
        )

    submit_calls = 0

    class FakeIntake:
        def submit(self, project_human_id, **kwargs):
            nonlocal submit_calls
            submit_calls += 1
            return _execution_result()

    monkeypatch.setattr("projectos.slack_chatgpt.IntakeService", lambda ctx: FakeIntake())

    reply = handle_chatgpt_slack_message(
        ctx,
        text="Execute it",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD,
        message_ts="9.0",
        user_id=SPONSOR,
    )
    assert reply is not None
    assert submit_calls == 1
    assert "EXECUTED" in reply["text"]
    with connection(ctx.db_path) as conn:
        proposal = get_latest_thread_proposal(
            conn, team_id=TEAM, channel_id=CHANNEL, thread_ts=THREAD, sponsor_user_id=SPONSOR
        )
        assert proposal is not None
        assert proposal.status == "completed"
        assert proposal.preview_result == RICH_PREVIEW
