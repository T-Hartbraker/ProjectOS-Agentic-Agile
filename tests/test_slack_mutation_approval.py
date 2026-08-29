"""Focused tests for proposal approval in mutation flow."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.chatgpt_proposals import create_proposal, get_latest_thread_proposal, save_proposal_preview
from projectos.chatgpt_store import upsert_chatgpt_thread
from projectos.db import connection
from projectos.intake import IntakeResult
from projectos.migrate import initialize_database
from projectos.services.context import ServiceContext
from projectos.slack_chatgpt import handle_chatgpt_slack_message

CHANNEL = "C0BSYCCDRST"
TEAM = "T1"
THREAD = "1787954712.918889"
SPONSOR = "U1"
PREVIEW = "*ProjectOS — PREVIEW*\n_No changes applied._"
EXECUTION = "*ProjectOS — EXECUTED*\n*Jobs created*\n- `JOB-1`"


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


@pytest.fixture(autouse=True)
def _env(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")


def test_approved_dispatches_work_request_and_persists_execution(tmp_path: Path, monkeypatch) -> None:
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
            instruction="Harmless documentation task only.",
        )
        save_proposal_preview(conn, proposal_id=record.proposal_id, preview_result=PREVIEW)

    class FakeIntake:
        def submit(self, project_human_id, **kwargs):
            return IntakeResult(
                status="submitted",
                project_human_id="PRJ-003",
                dry_run=False,
                jobs_created=["JOB-1"],
            )

    monkeypatch.setattr("projectos.slack_chatgpt.IntakeService", lambda ctx: FakeIntake())
    reply = handle_chatgpt_slack_message(
        ctx,
        text="Approved",
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD,
        message_ts="9.0",
        user_id=SPONSOR,
    )
    assert reply is not None
    assert "EXECUTED" in reply["text"]
    assert "JOB-1" in reply["text"]
    with connection(ctx.db_path) as conn:
        proposal = get_latest_thread_proposal(
            conn,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
            sponsor_user_id=SPONSOR,
        )
        assert proposal is not None
        assert proposal.status == "completed"
        assert proposal.preview_result == PREVIEW
        assert proposal.result_text
        assert "EXECUTED" in proposal.result_text
