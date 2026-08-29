"""Regression tests for governed ChatGPT mutation state integrity."""

from __future__ import annotations

from pathlib import Path

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
THREAD_TS = "1787954712.918889"
CHANNEL = "C0BSYCCDRST"
TEAM = "T1"
SPONSOR = "U1"
EXACT_INSTRUCTION = (
    "Propose a harmless governed change to PRJ-003: Update the project documentation "
    "with a new summary section reflecting recent quality review results. "
    "No code or process changes, purely documentation update."
)
RICH_PREVIEW = (
    "*ProjectOS — PREVIEW*\n"
    "_No changes applied. This is a read-only preview._\n"
    f"*Proposed change*\n{EXACT_INSTRUCTION}"
)
EXECUTION = "*ProjectOS — EXECUTED*\n*Jobs created*\n- `JOB-101`"


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


@pytest.fixture(autouse=True)
def _chatgpt_trigger_user(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PROJECTOS_SLACK_CHATGPT_USER_ID", CHATGPT_USER)


def test_live_fat_mutation_sequence_state_integrity(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    openai_calls = 0
    submit_calls = 0

    def fake_post(url, headers, body=None):
        nonlocal openai_calls
        if body is not None and isinstance(body, dict) and "input" in body:
            openai_calls += 1
            if openai_calls == 1:
                return _fake_openai_response("PRJ-003 is idle.", "resp-1")
            if openai_calls == 2:
                return _fake_openai_response("Submit new work when ready.", "resp-2")
            if openai_calls == 3:
                return _fake_openai_response(
                    "The execution is complete. Review the ProjectOS evidence below.",
                    "resp-3",
                )
            return _fake_openai_response("unexpected", f"resp-{openai_calls}")
        return {"ok": True}

    class FakeIntake:
        def preview(self, project_human_id, **kwargs):
            return IntakeResult(
                status="preview",
                project_human_id="PRJ-003",
                dry_run=True,
                decision_requests=[{"code": "untestable_acceptance", "question": "None"}],
            )

        def submit(self, project_human_id, **kwargs):
            nonlocal submit_calls
            submit_calls += 1
            return IntakeResult(
                status="submitted",
                project_human_id="PRJ-003",
                dry_run=False,
                jobs_created=["JOB-101"],
            )

    monkeypatch.setattr("projectos.slack_chatgpt.IntakeService", lambda ctx: FakeIntake())

    handle_chatgpt_slack_message(
        ctx,
        text=_mention("Give me a concise summary of PRJ-003."),
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD_TS,
        message_ts="1787954712.918889",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    handle_chatgpt_slack_message(
        ctx,
        text="What should I do next?",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD_TS,
        message_ts="1787954724.181609",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    with connection(ctx.db_path) as conn:
        upsert_chatgpt_thread(
            conn,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD_TS,
            sponsor_user_id=SPONSOR,
            project_human_id="PRJ-003",
            active=True,
            openai_response_id="resp-2",
        )
        record = create_proposal(
            conn,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD_TS,
            sponsor_user_id=SPONSOR,
            project_human_id="PRJ-003",
            intent="work_request",
            instruction=EXACT_INSTRUCTION,
        )
        save_proposal_preview(conn, proposal_id=record.proposal_id, preview_result=RICH_PREVIEW)

    summary_reply = handle_chatgpt_slack_message(
        ctx,
        text="What's the summary of the proposed change?",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD_TS,
        message_ts="1787954874.214799",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    assert summary_reply is not None
    assert EXACT_INSTRUCTION in summary_reply["text"]
    assert openai_calls == 2  # summary question is server-side; no extra OpenAI call

    dispatch_reply = handle_chatgpt_slack_message(
        ctx,
        text="Execute it",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD_TS,
        message_ts="1787954920.590929",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    assert dispatch_reply is not None
    assert "EXECUTED" in dispatch_reply["text"]
    assert submit_calls == 1
    assert openai_calls == 3  # execute adds interpretation call

    follow_up = handle_chatgpt_slack_message(
        ctx,
        text="what should i do next?",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD_TS,
        message_ts="1787955019.855589",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    assert follow_up is not None
    assert openai_calls == 4
    assert "approve" not in follow_up["text"].lower() or "evidence" in follow_up["text"].lower()

    preview_reply = handle_chatgpt_slack_message(
        ctx,
        text="Can you show me the preview of what was changed?",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD_TS,
        message_ts="1787955099.923259",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    assert preview_reply is not None
    assert "EXECUTED" in preview_reply["text"] or RICH_PREVIEW in preview_reply["text"]
    assert "resolved project before" not in preview_reply["text"].lower()
    assert openai_calls == 4

    with connection(ctx.db_path) as conn:
        thread = get_chatgpt_thread(
            conn, team_id=TEAM, channel_id=CHANNEL, thread_ts=THREAD_TS
        )
        assert thread is not None
        assert thread["project_human_id"] == "PRJ-003"
        proposals = list_thread_proposals(
            conn,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD_TS,
            sponsor_user_id=SPONSOR,
        )
        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal.status == "completed"
        assert proposal.instruction == EXACT_INSTRUCTION
        assert proposal.preview_result == RICH_PREVIEW
        assert proposal.result_text
        assert "EXECUTED" in proposal.result_text

    replay = handle_chatgpt_slack_message(
        ctx,
        text="Execute it",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD_TS,
        message_ts="1787955999.999999",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    assert replay is not None
    assert "no pending" in replay["text"].lower() or "does not look like" in replay["text"].lower()
    assert openai_calls == 4
