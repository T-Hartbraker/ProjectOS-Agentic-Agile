"""Enterprise Sponsor cockpit architecture tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.domain_events import EventContext, emit_projectos_event, ACTOR_PM
from projectos.event_dispatcher import dispatch_event_outbox
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.pm_agent import accept_sponsor_handoff, compose_server_handoff
from projectos.request_capability import classify_request
from projectos.services.context import ServiceContext
from projectos.slack_advisor_handoff import HandoffRequest, looks_like_handoff_trigger
from projectos.slack_chatgpt import handle_chatgpt_slack_message
from projectos.slack_activity_projector import flush_slack_activity_outbox
from projectos.slack_sponsor_context import build_sponsor_context
from projectos.sponsor_handoff import get_latest_thread_handoff
from projectos.chatgpt_store import upsert_chatgpt_thread
from projectos.store import add_slack_interface_channel

CHATGPT_USER = "UCHATGPT"
CHANNEL = "C0BSYCCDRST"
TEAM = "T1"
SPONSOR = "U1"
THREAD = "1788011691.418829"


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


def _fake_openai_response(text: str, response_id: str = "resp_cockpit") -> dict:
    return {
        "id": response_id,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PROJECTOS_SLACK_CHATGPT_USER_ID", CHATGPT_USER)
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")


def test_release_intent_classifies_without_model_block() -> None:
    cap = classify_request(
        text="I want to re-release the complete software package and installer and provide a download link."
    )
    assert cap.request_type == "RELEASE"
    assert cap.desired_outputs.get("installer") is True
    assert cap.desired_outputs.get("download_link") is True
    assert cap.constraints.get("use_approved_source") is True


def test_handoff_triggers_include_prepare_it_and_execute() -> None:
    assert looks_like_handoff_trigger("Prepare it.")
    assert looks_like_handoff_trigger("Execute!")
    assert looks_like_handoff_trigger("Yes. Send that to ProjectOS.")


def test_compose_server_handoff_without_model_block() -> None:
    handoff = compose_server_handoff(
        project_id="PRJ-003",
        sponsor_message="Prepare it.",
        thread_messages=[
            "I want to re-release the complete software package and installer and provide a download link."
        ],
    )
    assert handoff.project_id == "PRJ-003"
    assert handoff.action_type == "prepare_release"


def test_quality_facts_prevent_total_inference(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        sponsor_ctx = build_sponsor_context(
            ctx,
            conn,
            project_id="PRJ-003",
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_key=THREAD,
            sponsor_user_id=SPONSOR,
        )
    assert sponsor_ctx.quality_facts.get("gate_status") in {"PASSED", "FAILED", "PENDING"}
    assert "never_substitute" in sponsor_ctx.quality_facts.get("semantic_rules", {})
    text = sponsor_ctx.to_model_text()
    assert "QUALITY_FACTS_JSON" in text
    assert "NOT interchangeable" in text


def test_pm_accepts_handoff_once_and_creates_run(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        "projectos.pm_agent.orchestrate_release_capability",
        lambda *args, **kwargs: "mock evidence",
    )
    handoff = HandoffRequest(
        project_id="PRJ-003",
        objective="Re-release package",
        action_type="prepare_release",
        rationale="",
        scope="",
        constraints="{}",
        acceptance_intent="",
        exclusions="",
        source_conversation_summary="",
    )
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
        stored = get_latest_thread_handoff(
            conn, team_id=TEAM, channel_id=CHANNEL, thread_ts=THREAD
        )
        runs = conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
    assert stored is not None
    assert stored.status == "ACCEPTED_BY_PM"
    assert stored.handoff_id == result.handoff_id
    assert result.run_id.startswith("RUN-")
    assert int(runs) == 1


def test_slack_thread_handoff_without_model_block(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    thread = "1788011691.999999"
    with connection(ctx.db_path) as conn:
        upsert_chatgpt_thread(
            conn,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=thread,
            sponsor_user_id=SPONSOR,
            project_human_id="PRJ-003",
            active=True,
        )
        insert_chatgpt_message = __import__(
            "projectos.chatgpt_store", fromlist=["insert_chatgpt_message"]
        ).insert_chatgpt_message
        insert_chatgpt_message(
            conn,
            team_id=TEAM,
            channel_id=CHANNEL,
            thread_ts=thread,
            message_ts="1788011691.100000",
            user_id=SPONSOR,
            role="sponsor",
            text="I want to re-release the complete software package and installer and provide a download link.",
        )
    monkeypatch.setattr(
        "projectos.openai_client.default_http_post",
        lambda url, headers, body: _fake_openai_response(
            "Let me prepare the work request handoff for ProjectOS..."
        ),
    )
    monkeypatch.setattr(
        "projectos.pm_agent.orchestrate_release_capability",
        lambda *args, **kwargs: "*Release evidence* stub",
    )

    reply = handle_chatgpt_slack_message(
        ctx,
        text=_mention("Prepare it."),
        channel_id=CHANNEL,
        team_id=TEAM,
        thread_ts=thread,
        message_ts="1788011691.500000",
        user_id=SPONSOR,
    )
    assert reply is not None
    assert "HANDOFF ACCEPTED" in str(reply.get("text") or "")
    with connection(ctx.db_path) as conn:
        handoff = get_latest_thread_handoff(
            conn, team_id=TEAM, channel_id=CHANNEL, thread_ts=thread
        )
    assert handoff is not None
    assert handoff.request_type == "RELEASE"


def test_outbox_idempotency(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        emit_projectos_event(
            conn,
            ctx=EventContext(
                project_id="PRJ-003",
                run_id="RUN-1",
                slack_channel_id=CHANNEL,
                slack_thread_ts=THREAD,
            ),
            event_type="HANDOFF_ACCEPTED",
            summary="hello",
            actor_id=ACTOR_PM,
        )
        first = conn.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0]
        emit_projectos_event(
            conn,
            ctx=EventContext(
                project_id="PRJ-003",
                run_id="RUN-1",
                slack_channel_id=CHANNEL,
                slack_thread_ts=THREAD,
            ),
            event_type="WORK_STARTED",
            summary="hello2",
            actor_id=ACTOR_PM,
        )
        second = conn.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0]
    assert int(first) == 1
    assert int(second) == 2


def test_handoff_activity_and_outbox_share_transaction(tmp_path: Path) -> None:
    from projectos.agent_activity import record_sponsor_activity, ThreadCorrelation
    from projectos.db import connect

    ctx = _ctx(tmp_path)
    conn = connect(ctx.db_path)
    thread = ThreadCorrelation(
        project_id="PRJ-003",
        handoff_id="HND-TX",
        run_id="RUN-TX",
        team_id=TEAM,
        channel_id=CHANNEL,
        thread_ts=THREAD,
    )
    try:
        record_sponsor_activity(
            conn,
            thread=thread,
            event_type="HANDOFF_ACCEPTED",
            summary="tx test",
            actor_role="PM Agent",
            detail_level="milestone",
        )
        conn.rollback()
    finally:
        conn.close()
    with connection(ctx.db_path) as verify:
        events = verify.execute("SELECT COUNT(*) FROM projectos_events").fetchone()[0]
        outbox = verify.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0]
        legacy = verify.execute("SELECT COUNT(*) FROM slack_activity_outbox").fetchone()[0]
    assert int(events) == 0
    assert int(outbox) == 0
    assert int(legacy) == 0


def test_outbox_restart_delivers_once(tmp_path: Path, monkeypatch) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        emit_projectos_event(
            conn,
            ctx=EventContext(
                project_id="PRJ-003",
                slack_channel_id=CHANNEL,
                slack_thread_ts=THREAD,
            ),
            event_type="PACKAGE_COMPLETED",
            summary="restart test",
            actor_id=ACTOR_PM,
        )
    calls = []

    def fake_post(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr("projectos.event_dispatcher.post_message", fake_post)
    first = flush_slack_activity_outbox(ctx.db_path)
    second = flush_slack_activity_outbox(ctx.db_path)
    assert first["delivered"] == 1
    assert second["delivered"] == 0
    assert len(calls) == 1
    with connection(ctx.db_path) as conn:
        row = conn.execute(
            "SELECT status FROM event_outbox WHERE subscriber = 'slack' LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row["status"] == "delivered"


def test_thread_correlation_metadata_on_event(tmp_path: Path, monkeypatch) -> None:
    from projectos.agent_activity import ThreadCorrelation, record_sponsor_activity

    ctx = _ctx(tmp_path)
    thread = ThreadCorrelation(
        project_id="PRJ-003",
        handoff_id="HND-CORR",
        run_id="RUN-CORR",
        team_id=TEAM,
        channel_id=CHANNEL,
        thread_ts=THREAD,
    )
    with connection(ctx.db_path) as conn:
        record_sponsor_activity(
            conn,
            thread=thread,
            event_type="WORK_STARTED",
            summary="corr",
            actor_role="PM Agent",
            project_to_slack=False,
        )
        row = conn.execute(
            "SELECT metadata_json FROM agent_activity_events ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
    import json

    meta = json.loads(str(row["metadata_json"]))
    assert meta["slack_channel_id"] == CHANNEL
    assert meta["slack_thread_ts"] == THREAD
    assert meta["handoff_id"] == "HND-CORR"
    assert meta["run_id"] == "RUN-CORR"

    with connection(ctx.db_path) as conn:
        emit_projectos_event(
            conn,
            ctx=EventContext(
                project_id="PRJ-003",
                slack_channel_id=CHANNEL,
                slack_thread_ts=THREAD,
            ),
            event_type="WORK_STARTED",
            summary="flush test",
            actor_id=ACTOR_PM,
        )
    monkeypatch.setattr(
        "projectos.event_dispatcher.post_message",
        lambda **kwargs: {"ok": True},
    )
    stats = flush_slack_activity_outbox(ctx.db_path)
    assert stats["delivered"] == 1
    with connection(ctx.db_path) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE status = 'pending'"
        ).fetchone()[0]
    assert int(pending) == 0
