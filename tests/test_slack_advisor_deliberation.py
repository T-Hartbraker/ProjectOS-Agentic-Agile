"""Regression tests for ChatGPT Advisor deliberation layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.chatgpt_proposals import list_pending_proposals, list_thread_proposals
from projectos.chatgpt_store import upsert_chatgpt_thread
from projectos.db import connection
from projectos.intake import IntakeResult
from projectos.migrate import initialize_database
from projectos.services.context import ServiceContext
from projectos.slack_chatgpt import CHATGPT_PREFIX, PROJECTOS_PREFIX, handle_chatgpt_slack_message
from projectos.store import add_slack_interface_channel

CHATGPT_USER = "UCHATGPT"
CHANNEL = "C0BSYCCDRST"
TEAM = "T1"
SPONSOR = "U1"
THREAD = "1788000000.000000"


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
        upsert_chatgpt_thread(
            conn,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
            sponsor_user_id=SPONSOR,
            project_human_id="PRJ-003",
            active=True,
        )
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def _fake_openai_response(text: str, response_id: str = "resp_seq") -> dict:
    return {
        "id": response_id,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PROJECTOS_SLACK_CHATGPT_USER_ID", CHATGPT_USER)


def test_deliberation_sequence_no_proposal_until_handoff(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    openai_calls = 0
    preview_calls = 0
    submit_calls = 0
    responses = {
        1: (
            "PRJ-003 is idle with ITER-002 as the current iteration. Quality reviews are complete. "
            "Given that, I would not start another implementation iteration yet.\n\n"
            "*Recommendation*\n"
            "Focus on closeout: document what was delivered, capture quality outcomes, and decide "
            "whether ITER-002 is truly complete before opening new build work."
        ),
        2: (
            "You have several alternatives:\n"
            "1. *Documentation closeout* — lightweight, low risk.\n"
            "2. *New iteration* — only if product scope remains.\n"
            "3. *Release path* — if verification is done.\n"
            "4. *Pause* — if priorities shifted.\n\n"
            "Given your concern about process bloat, option 1 is the best fit."
        ),
        3: (
            "Agreed — we can keep this minimal.\n\n"
            "*Proposed outcome (recommendation, not yet ProjectOS state)*\n"
            "One documentation-only closeout item for ITER-002 summarizing delivered work and QA."
        ),
        4: (
            "I would create one governed documentation work item tied to ITER-002. "
            "No code changes, no new implementation jobs.\n\n"
            "```projectos_handoff\n"
            '{"objective":"Create a lightweight ITER-002 closeout documentation work item",'
            '"action_type":"work_request","scope":"summarize delivered work and QA outcomes",'
            '"constraints":"no application code changes","exclusions":"no new implementation iteration",'
            '"acceptance_intent":"one documentation work item associated with ITER-002",'
            '"source_conversation_summary":"Sponsor chose documentation closeout over new iteration"}\n'
            "```"
        ),
        5: (
            "I'll prepare the ProjectOS handoff now.\n\n"
            "```projectos_handoff\n"
            '{"objective":"Create a lightweight ITER-002 closeout documentation work item",'
            '"action_type":"work_request","scope":"summarize delivered work and QA outcomes",'
            '"constraints":"no application code changes","exclusions":"no new implementation iteration",'
            '"acceptance_intent":"one documentation work item associated with ITER-002",'
            '"source_conversation_summary":"Sponsor chose documentation closeout over new iteration"}\n'
            "```"
        ),
        6: "ProjectOS created JOB-900 as intended — a single documentation path without code changes.",
    }

    class FakeIntake:
        def preview(self, project_human_id, **kwargs):
            nonlocal preview_calls
            preview_calls += 1
            return IntakeResult(
                status="preview",
                project_human_id=project_human_id,
                dry_run=True,
                expected_jobs=[{"human_id": "JOB-900", "queue": "PM"}],
            )

        def submit(self, project_human_id, **kwargs):
            nonlocal submit_calls
            submit_calls += 1
            return IntakeResult(
                status="submitted",
                project_human_id=project_human_id,
                dry_run=False,
                jobs_created=["JOB-900"],
            )

    monkeypatch.setattr("projectos.slack_chatgpt.IntakeService", lambda ctx: FakeIntake())

    def fake_post(url, headers, body=None):
        nonlocal openai_calls
        if body is not None and isinstance(body, dict) and "input" in body:
            openai_calls += 1
            return _fake_openai_response(responses.get(openai_calls, "ok"), f"resp-{openai_calls}")
        return {"ok": True}

    turns = [
        "What should we do next on PRJ-003?",
        "I'm not sure I want another iteration. What alternatives do I have?",
        "I like the documentation closeout idea, but I don't want process bloat.",
        "What exactly would you create?",
    ]
    for idx, message in enumerate(turns, start=1):
        reply = handle_chatgpt_slack_message(
            ctx,
            text=_mention(message) if idx == 1 else message,
            channel_id=CHANNEL,
            team_id=TEAM,
            thread_ts=THREAD,
            message_ts=f"{THREAD}.{idx}",
            user_id=SPONSOR,
            http_post=fake_post,
        )
        assert reply is not None
        assert CHATGPT_PREFIX in reply["text"] or "*ChatGPT Advisor:*" in reply["text"]

    with connection(ctx.db_path) as conn:
        assert list_thread_proposals(
            conn, team_id=TEAM, channel_id=CHANNEL, thread_ts=THREAD, sponsor_user_id=SPONSOR
        ) == []

    handoff_reply = handle_chatgpt_slack_message(
        ctx,
        text="Okay. Have ProjectOS do that.",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD,
        message_ts=f"{THREAD}.5",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    assert handoff_reply is not None
    assert "PREVIEW" in handoff_reply["text"]
    assert preview_calls == 1
    assert submit_calls == 0
    assert openai_calls == 5

    with connection(ctx.db_path) as conn:
        pending = list_pending_proposals(
            conn, team_id=TEAM, channel_id=CHANNEL, thread_ts=THREAD, sponsor_user_id=SPONSOR
        )
        assert len(pending) == 1
        assert pending[0].preview_result

    approve = handle_chatgpt_slack_message(
        ctx,
        text="Approved",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD,
        message_ts=f"{THREAD}.approve",
        user_id=SPONSOR,
        http_post=fake_post,
    )
    assert approve is not None
    assert PROJECTOS_PREFIX in approve["text"]
    assert submit_calls == 1
    assert openai_calls == 6
