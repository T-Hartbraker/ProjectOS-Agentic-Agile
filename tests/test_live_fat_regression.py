"""Live FAT regression — format_releases, handoff resilience, QA semantics."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.advisor_errors import PROJECTOS_FORMATTER_ERROR, classify_advisor_exception
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.qa_semantics import collect_assurance_facts
from projectos.services.context import ServiceContext
from projectos.slack_chatgpt import handle_chatgpt_slack_message, try_handle_chatgpt_event
from projectos.sponsor_action_intent import detect_sponsor_action_intent
from projectos.sponsor_handoff import get_latest_thread_handoff
from projectos.sponsor_query import SponsorQueryService
from projectos.chatgpt_store import upsert_chatgpt_thread
from projectos.store import add_slack_interface_channel

CHATGPT_USER = "UCHATGPT"
CHANNEL = "C0BSYCCDRST"
TEAM = "T1"
SPONSOR = "U1"
THREAD_1 = "1788018533.754629"
THREAD_2 = "1788018834.681129"


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


def _fake_openai_response(text: str, response_id: str = "resp_live") -> dict:
    return {
        "id": response_id,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PROJECTOS_SLACK_CHATGPT_USER_ID", CHATGPT_USER)
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")


def test_format_releases_regression_via_release_intent_message(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    release_msg = (
        "I want ProjectOS to re-release the complete current software package "
        "and installer and give me the finished download link."
    )
    text = SponsorQueryService(ctx).query_for_advisor("PRJ-003", "releases", raw_text=release_msg)
    assert "PRJ-003" in text


def test_formatter_exception_classified_not_openai() -> None:
    err = classify_advisor_exception(TypeError("format_releases() missing 1 required positional argument: 'raw_text'"))
    assert err.error_class == PROJECTOS_FORMATTER_ERROR
    assert "OpenAI" not in err.sponsor_message


def test_qa_semantics_do_not_substitute_jobs_for_evidence_rows(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    facts = collect_assurance_facts(ctx, "PRJ-003")
    assert "qa_jobs_total" in facts
    assert "assurance_evidence_rows_total" in facts
    assert facts["semantic_rules"]["never_substitute"]


def test_release_intent_detected_without_handoff_trigger_phrase() -> None:
    intent = detect_sponsor_action_intent(
        "I want ProjectOS to re-release the complete current software package and installer."
    )
    assert intent.requires_pm_handoff is True
    assert intent.request_type == "RELEASE"


def test_live_fat_conversation_flow_handoff_survives(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    calls: list[str] = []

    def fake_post(url, headers, body=None):
        if body and isinstance(body, dict) and "input" in body:
            calls.append(str(body.get("input", "")))
            if len(calls) == 1:
                return _fake_openai_response(
                    "For PRJ-003, next steps include validating QA and preparing a governed release handoff."
                )
            if len(calls) == 2:
                return _fake_openai_response(
                    "JOB-P2-RELEASE__RETRY-1 was a release retry attempt. "
                    "ProjectOS does not contain enough evidence to determine the exact blocker cause "
                    "if no last_error is recorded."
                )
            return _fake_openai_response("Understood. I will prepare the release handoff.")
        return {"ok": True}

    monkeypatch.setattr("projectos.openai_client.default_http_post", fake_post)
    monkeypatch.setattr(
        "projectos.pm_agent.orchestrate_release_capability",
        lambda *args, **kwargs: "mock release evidence",
    )

    with connection(ctx.db_path) as conn:
        upsert_chatgpt_thread(
            conn,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD_1,
            sponsor_user_id=SPONSOR,
            project_human_id="PRJ-003",
            active=True,
        )

    r1 = handle_chatgpt_slack_message(
        ctx,
        text=_mention("assess PRJ-003. What should the next steps be?"),
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD_1,
        message_ts="1.1",
        user_id=SPONSOR,
    )
    assert r1 is not None
    assert "unavailable" not in str(r1.get("text", "")).lower() or "OpenAI integration" not in str(r1.get("text", ""))

    r2 = handle_chatgpt_slack_message(
        ctx,
        text=_mention(
            "Give me details on JOB-P2-RELEASE__RETRY-1. "
            "What was this, what blocked it, and is this still remaining work?"
        ),
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD_1,
        message_ts="1.2",
        user_id=SPONSOR,
    )
    assert r2 is not None
    ctx_text = str(r2.get("text", ""))
    assert "format_releases" not in ctx_text

    r3 = handle_chatgpt_slack_message(
        ctx,
        text=_mention(
            "I want ProjectOS to re-release the complete current software package "
            "and installer and give me the finished download link."
        ),
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD_1,
        message_ts="1.3",
        user_id=SPONSOR,
    )
    assert r3 is not None
    assert "HANDOFF" in str(r3.get("text", "")).upper() or "HANDOFF" in str(r3).upper()

    with connection(ctx.db_path) as conn:
        handoff = get_latest_thread_handoff(conn, team_id=TEAM, channel_id=CHANNEL, thread_ts=THREAD_1)
        run_row = conn.execute("SELECT run_id FROM execution_runs ORDER BY rowid DESC LIMIT 1").fetchone()
        events = conn.execute(
            "SELECT COUNT(*) FROM projectos_events WHERE event_type = 'HANDOFF_ACCEPTED'"
        ).fetchone()[0]
        outbox = conn.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0]
        legacy = conn.execute("SELECT COUNT(*) FROM slack_activity_outbox WHERE status = 'pending'").fetchone()[0]
    assert handoff is not None
    assert handoff.handoff_id.startswith("HND-")
    assert run_row is not None
    assert str(run_row["run_id"]).startswith("RUN-")
    assert int(events) >= 1
    assert int(outbox) >= 1
    assert int(legacy) == 0


def test_fresh_thread_inherits_channel_project_context(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        "projectos.openai_client.default_http_post",
        lambda url, headers, body=None: _fake_openai_response("Context acknowledged for PRJ-003."),
    )
    with connection(ctx.db_path) as conn:
        upsert_chatgpt_thread(
            conn,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=THREAD_1,
            sponsor_user_id=SPONSOR,
            project_human_id="PRJ-003",
            active=True,
        )
    handle_chatgpt_slack_message(
        ctx,
        text=_mention("assess PRJ-003 status"),
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD_1,
        message_ts="2.1",
        user_id=SPONSOR,
    )
    reply = handle_chatgpt_slack_message(
        ctx,
        text=_mention("What are the next steps?"),
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=THREAD_2,
        message_ts="2.2",
        user_id=SPONSOR,
    )
    assert reply is not None
    assert "Which ProjectOS project" not in str(reply.get("text", ""))
