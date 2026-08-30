"""Sponsor execution authority and redundant-approval regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.chatgpt_proposals import is_work_mutation
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.pm_agent import accept_sponsor_handoff
from projectos.services.context import ServiceContext
from projectos.slack_advisor_handoff import HandoffRequest
from projectos.sponsor_execution_authority import classify_sponsor_execution_authority
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
THREAD = "300.0"
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
    with connection(db) as conn:
        add_slack_interface_channel(conn, channel_id=CHANNEL, team_id=TEAM, is_default=True)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def test_fat_message_classifies_as_execution_authorized() -> None:
    authority = classify_sponsor_execution_authority(
        FAT_MESSAGE,
        explicit_new_project=True,
    )
    assert authority.execution_authorized is True
    assert authority.authority_source == "explicit_new_project"
    assert authority.sponsor_authority == "approved"


def test_discussion_request_is_not_execution_authorized() -> None:
    authority = classify_sponsor_execution_authority(
        "What should we do next for the calculator project?"
    )
    assert authority.execution_authorized is False
    assert authority.authority_source == "none"


@pytest.mark.parametrize(
    "text",
    [
        "Give me some ideas for improving the CLI.",
        "Assess this project and recommend next steps.",
    ],
)
def test_discussion_variants_remain_unauthorized(text: str) -> None:
    assert classify_sponsor_execution_authority(text).execution_authorized is False


def test_handoff_trigger_grants_execution_authority() -> None:
    authority = classify_sponsor_execution_authority("Prepare it.")
    assert authority.execution_authorized is True
    assert authority.authority_source == "handoff_trigger"


def test_unauthorized_work_handoff_pauses_for_sponsor(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    handoff = HandoffRequest(
        project_id="PRJ-003",
        objective="What should we do next for the calculator?",
        action_type="work_request",
        rationale="",
        scope="",
        constraints="{}",
        acceptance_intent="",
        exclusions="",
        source_conversation_summary="",
    )
    assert is_work_mutation(handoff.action_type)
    with connection(ctx.db_path) as conn:
        result = accept_sponsor_handoff(
            ctx,
            conn,
            handoff=handoff,
            project_id="PRJ-003",
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD,
            sponsor_user_id=SPONSOR,
        )
        run = conn.execute(
            "SELECT status, result_summary FROM execution_runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
        proposals = conn.execute(
            "SELECT COUNT(*) AS total FROM slack_chatgpt_proposals WHERE thread_ts = ?",
            (THREAD,),
        ).fetchone()
    assert run["status"] == "WAITING_FOR_SPONSOR"
    assert "Sponsor approval required before execution" in str(run["result_summary"] or "")
    assert int(proposals["total"]) == 1
