"""ChatGPT proposal state machine and private-channel routing tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.chatgpt_proposals import (
    approve_pending_proposal,
    create_proposal,
    list_pending_proposals,
    mark_proposal_dispatched,
    parse_proposal_request,
    proposal_to_execution_payload,
    save_proposal_preview,
)
from projectos.db import connection
from projectos.intake import IntakeResult
from projectos.migrate import initialize_database
from projectos.services.context import ServiceContext
from projectos.slack_chatgpt import (
    CHATGPT_PREFIX,
    PROJECTOS_PREFIX,
    handle_chatgpt_slack_message,
    is_chatgpt_addressed,
    should_route_to_chatgpt,
    try_handle_chatgpt_event,
)
from projectos.slack_event_routing import (
    is_interface_channel_message_event,
    is_private_channel_message_event,
)
from projectos.slack_socket import handle_events_api_payload, process_socket_envelope
from projectos.store import add_slack_interface_channel

CHATGPT_USER = "UCHATGPT"


def _mention(text: str) -> str:
    return f"<@{CHATGPT_USER}|ChatGPT> {text}".strip()


@pytest.fixture(autouse=True)
def _chatgpt_trigger_user(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_CHATGPT_USER_ID", CHATGPT_USER)


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
        add_slack_interface_channel(conn, channel_id="G_PRIVATE", team_id="T1", is_default=True)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def _fake_openai_response(text: str, response_id: str = "resp_test") -> dict:
    return {
        "id": response_id,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


def _seed_preview(conn, *, proposal_id: str, text: str = "*ProjectOS — PREVIEW*\n_No changes applied._") -> None:
    save_proposal_preview(conn, proposal_id=proposal_id, preview_result=text)


def _mock_intake_preview(monkeypatch) -> None:
    class FakeIntake:
        def preview(self, project_human_id, **kwargs):
            return IntakeResult(
                status="preview",
                project_human_id=project_human_id,
                dry_run=True,
            )

    monkeypatch.setattr("projectos.slack_chatgpt.IntakeService", lambda ctx: FakeIntake())


def test_private_channel_message_event_detected() -> None:
    event = {"type": "message", "channel_type": "group", "channel": "G_PRIVATE", "ts": "1.0"}
    assert is_private_channel_message_event(event)
    assert is_interface_channel_message_event(event)


def test_should_route_private_channel_thread_reply(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    from projectos.openai_tokens import reload_openai_tokens

    reload_openai_tokens()
    event = {"type": "message", "channel_type": "group", "channel": "G_PRIVATE", "thread_ts": "1.0"}
    assert should_route_to_chatgpt(
        text="follow up",
        event=event,
        thread_state={"active": True},
        projectos_thread_active=False,
    )


def test_parse_proposal_request_rejects_authorization_block() -> None:
    text = """```projectos_proposal
{"intent":"status","instruction":"check","authorization":{"type":"explicit_sponsor_approval"}}
```"""
    parsed = parse_proposal_request(text)
    assert parsed is not None
    assert parsed.intent == "status"


def test_proposal_creation_without_execution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    _mock_intake_preview(monkeypatch)
    ctx = _ctx(tmp_path)

    def fake_post(url, headers, body):
        return _fake_openai_response(
            "I'll prepare the handoff.\n```projectos_handoff\n"
            '{"objective":"validation readiness","action_type":"work_request"}\n```'
        )

    reply = handle_chatgpt_slack_message(
        ctx,
        text=_mention("Have ProjectOS validate readiness for PRJ-003"),
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="100.0",
        message_ts="101.0",
        user_id="U1",
        http_post=fake_post,
    )
    assert PROJECTOS_PREFIX in reply["text"]
    assert "PREVIEW" in reply["text"]
    with connection(ctx.db_path) as conn:
        pending = list_pending_proposals(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
        )
        assert len(pending) == 1
        assert pending[0].instruction == "validation readiness"
        assert pending[0].preview_result
        assert pending[0].status == "pending"


def test_approval_executes_exact_persisted_proposal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        from projectos.chatgpt_store import upsert_chatgpt_thread

        upsert_chatgpt_thread(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            active=True,
            openai_response_id="resp_prior",
        )
        record = create_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            intent="work_request",
            instruction="EXACT_STORED_INSTRUCTION",
        )
        _seed_preview(conn, proposal_id=record.proposal_id)

    class FakeIntake:
        def submit(self, project_human_id, **kwargs):
            return IntakeResult(
                status="submitted",
                project_human_id=project_human_id,
                dry_run=False,
                jobs_created=["JOB-1"],
            )

    monkeypatch.setattr("projectos.slack_chatgpt.IntakeService", lambda ctx: FakeIntake())

    reply = handle_chatgpt_slack_message(
        ctx,
        text="go ahead",
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="100.0",
        message_ts="202.0",
        user_id="U1",
    )
    assert reply is not None
    assert PROJECTOS_PREFIX in reply["text"]
    assert "EXECUTED" in reply["text"]
    assert "EXACT_STORED_INSTRUCTION" not in reply["text"]


def test_model_post_approval_instruction_change_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        proposal = create_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            intent="work_request",
            instruction="ORIGINAL",
        )
        _seed_preview(conn, proposal_id=proposal.proposal_id)
        approved, _ = approve_pending_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            approval_message_ts="200.0",
            approval_text="approved",
        )
        assert approved is not None
        payload = proposal_to_execution_payload(approved)
        assert payload["instruction"] == "ORIGINAL"
        assert proposal.proposal_id == approved.proposal_id


def test_approval_by_wrong_user_rejected(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        create_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            intent="work_preview",
            instruction="ORIGINAL",
        )
        approved, error = approve_pending_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U2",
            project_human_id="PRJ-003",
            approval_message_ts="200.0",
            approval_text="approved",
        )
        assert approved is None
        assert error


def test_approval_after_project_context_switch_rejected(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        create_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            intent="work_preview",
            instruction="ORIGINAL",
        )
        approved, error = approve_pending_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-999",
            approval_message_ts="200.0",
            approval_text="approved",
        )
        assert approved is None
        assert "context changed" in (error or "").lower()


def test_duplicate_dispatch_blocked(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        proposal = create_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            intent="work_request",
            instruction="ORIGINAL",
        )
        _seed_preview(conn, proposal_id=proposal.proposal_id)
        approved, _ = approve_pending_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            approval_message_ts="200.0",
            approval_text="approved",
        )
        assert approved is not None
        assert mark_proposal_dispatched(conn, proposal_id=proposal.proposal_id)
        assert not mark_proposal_dispatched(conn, proposal_id=proposal.proposal_id)


def test_expired_proposal_rejected(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        proposal = create_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            intent="work_preview",
            instruction="ORIGINAL",
        )
        expired = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
        conn.execute(
            "UPDATE slack_chatgpt_proposals SET expires_at = ? WHERE proposal_id = ?",
            (expired.isoformat().replace("+00:00", "Z"), proposal.proposal_id),
        )
        pending = list_pending_proposals(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
        )
        assert pending == []


def test_ambiguous_multiple_pending_proposals(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        first = create_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            intent="work_preview",
            instruction="ONE",
        )
        second = create_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            intent="work_preview",
            instruction="TWO",
        )
        assert second.proposal_id == first.proposal_id
        pending = list_pending_proposals(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
        )
        assert len(pending) == 1


def test_private_channel_thread_reply_routing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        from projectos.chatgpt_store import upsert_chatgpt_thread

        upsert_chatgpt_thread(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            active=True,
        )

    payload = {
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": "G_PRIVATE",
            "channel_type": "group",
            "thread_ts": "100.0",
            "ts": "101.0",
            "user": "U1",
            "text": "Can you elaborate?",
        },
    }

    def fake_post(url, headers, body):
        return _fake_openai_response("Sure.")

    reply = try_handle_chatgpt_event(ctx, event=payload["event"], payload=payload, http_post=fake_post)
    assert reply is not None
    assert CHATGPT_PREFIX in reply["text"]


def test_private_channel_app_mention_routing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    payload = {
        "team_id": "T1",
        "event": {
            "type": "app_mention",
            "channel": "G_PRIVATE",
            "channel_type": "group",
            "ts": "102.0",
            "user": "U1",
            "text": f"<@UBOT> {_mention('thoughts?')}",
        },
    }

    def fake_post(url, headers, body):
        return _fake_openai_response("Here are my thoughts.")

    reply = handle_events_api_payload(ctx, payload, bot_user_id="UBOT", http_post=fake_post)
    assert reply is not None
    assert CHATGPT_PREFIX in reply["text"]


def test_bot_loop_prevention(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    payload = {
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": "G_PRIVATE",
            "channel_type": "group",
            "thread_ts": "100.0",
            "ts": "103.0",
            "user": "UBOT",
            "text": f"{CHATGPT_PREFIX} prior",
        },
    }
    assert try_handle_chatgpt_event(ctx, event=payload["event"], payload=payload) is None


def test_openai_input_uses_chaining_not_full_history(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    captured: list[dict] = []

    def fake_post(url, headers, body):
        captured.append(body or {})
        return _fake_openai_response("ok")

    with connection(ctx.db_path) as conn:
        from projectos.chatgpt_store import insert_chatgpt_message, upsert_chatgpt_thread

        upsert_chatgpt_thread(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            sponsor_user_id="U1",
            active=True,
            openai_response_id="resp_chain",
        )
        insert_chatgpt_message(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="100.0",
            message_ts="1.0",
            user_id="U1",
            role="sponsor",
            text="older message that must not be replayed in full",
        )

    handle_chatgpt_slack_message(
        ctx,
        text=_mention("next"),
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="100.0",
        message_ts="2.0",
        user_id="U1",
        http_post=fake_post,
    )
    assert captured
    body = captured[0]
    assert body.get("previous_response_id") == "resp_chain"
    assert "older message that must not be replayed in full" not in str(body.get("input"))


def test_duplicate_envelope_idempotency(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _ctx(tmp_path)
    envelope = {
        "envelope_id": "env-private-1",
        "type": "events_api",
        "payload": {
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "channel": "G_PRIVATE",
                "channel_type": "group",
                "ts": "104.0",
                "user": "U1",
                "text": f"<@UBOT> {_mention('hello')}",
            },
        },
    }

    def fake_post(url, headers, body=None):
        if body is None:
            return {"ok": True}
        if "chat.postMessage" in url:
            return {"ok": True}
        return _fake_openai_response("Hello.")

    first = process_socket_envelope(ctx, envelope, http_post=fake_post, bot_user_id="UBOT")
    second = process_socket_envelope(ctx, envelope, http_post=fake_post, bot_user_id="UBOT")
    assert first["duplicate"] is False
    assert second["duplicate"] is True


def test_is_chatgpt_addressed_by_slack_user_mention() -> None:
    assert is_chatgpt_addressed(f"<@{CHATGPT_USER}|ChatGPT> hello")
    assert is_chatgpt_addressed(f"<@{CHATGPT_USER}> hello")
    assert not is_chatgpt_addressed("@ChatGPT hello")
    assert not is_chatgpt_addressed("hello there")


def test_message_groups_with_chatgpt_mention_invokes_bridge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    captured: list[str] = []

    def fake_post(url, headers, body):
        captured.append(str((body or {}).get("input") or ""))
        return _fake_openai_response("Concise summary ready.")

    payload = {
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": "G_PRIVATE",
            "ts": "200.0",
            "user": "U1",
            "text": _mention("give me a concise summary of PRJ-003."),
        },
    }
    reply = handle_events_api_payload(ctx, payload, bot_user_id="UBOT", http_post=fake_post)
    assert reply is not None
    assert CHATGPT_PREFIX in reply["text"]
    assert PROJECTOS_PREFIX in reply["text"]
    assert captured
    assert CHATGPT_USER not in captured[0]
    assert "give me a concise summary of PRJ-003." in captured[0]
    assert "ID: PRJ-003" in captured[0]


def test_chatgpt_mention_stripped_before_openai_prompt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    captured: list[dict] = []

    def fake_post(url, headers, body):
        captured.append(body or {})
        return _fake_openai_response("ok")

    handle_chatgpt_slack_message(
        ctx,
        text=_mention("status check"),
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="300.0",
        message_ts="301.0",
        user_id="U1",
        http_post=fake_post,
    )
    assert captured
    assert CHATGPT_USER not in str(captured[0].get("input"))
    assert "status check" in str(captured[0].get("input"))


def test_unrelated_message_ignored_without_chatgpt_thread(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    payload = {
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": "G_PRIVATE",
            "channel_type": "group",
            "ts": "400.0",
            "user": "U1",
            "text": "just a normal message",
        },
    }
    assert handle_events_api_payload(ctx, payload, bot_user_id="UBOT") is None


def test_installed_chatgpt_bot_user_messages_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    payload = {
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": "G_PRIVATE",
            "channel_type": "group",
            "thread_ts": "100.0",
            "ts": "401.0",
            "user": CHATGPT_USER,
            "text": "I am the installed ChatGPT app",
        },
    }
    assert handle_events_api_payload(ctx, payload, bot_user_id="UBOT") is None


def _chatgpt_event_payload(
    *,
    event_id: str = "Ev-test",
    ts: str = "500.0",
    text: str,
    event_type: str = "message",
) -> dict:
    return {
        "team_id": "T1",
        "event_id": event_id,
        "event": {
            "type": event_type,
            "channel": "G_PRIVATE",
            "channel_type": "group",
            "ts": ts,
            "user": "U1",
            "text": text,
        },
    }


def test_same_event_id_different_envelopes_one_openai_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _ctx(tmp_path)
    openai_calls = 0

    def fake_post(url, headers, body=None):
        nonlocal openai_calls
        if body is not None and isinstance(body, dict) and "input" in body:
            openai_calls += 1
            return _fake_openai_response("One response only.")
        if "chat.postMessage" in str(url):
            return {"ok": True}
        return {"ok": True}

    payload = _chatgpt_event_payload(text=_mention("summarize PRJ-003"))
    first = process_socket_envelope(
        ctx,
        {"envelope_id": "env-a", "type": "events_api", "payload": payload},
        http_post=fake_post,
        bot_user_id="UBOT",
    )
    second = process_socket_envelope(
        ctx,
        {"envelope_id": "env-b", "type": "events_api", "payload": payload},
        http_post=fake_post,
        bot_user_id="UBOT",
    )
    assert first["duplicate"] is False
    assert first["reply"] is not None
    assert second["duplicate"] is False
    assert second["reply"] is None
    assert openai_calls == 1


def test_message_and_app_mention_same_ts_one_openai_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    openai_calls = 0

    def fake_post(url, headers, body=None):
        nonlocal openai_calls
        if body is not None and isinstance(body, dict) and "input" in body:
            openai_calls += 1
            return _fake_openai_response(
                "Summary.\n```projectos_proposal\n"
                '{"intent":"work_preview","instruction":"summarize work"}\n```'
            )
        return _fake_openai_response("unused")

    message_payload = _chatgpt_event_payload(
        event_id="Ev-message",
        ts="501.0",
        text=_mention("summarize PRJ-003"),
        event_type="message",
    )
    mention_payload = _chatgpt_event_payload(
        event_id="Ev-mention",
        ts="501.0",
        text=f"<@UBOT> {_mention('summarize PRJ-003')}",
        event_type="app_mention",
    )
    first = handle_events_api_payload(ctx, message_payload, bot_user_id="UBOT", http_post=fake_post)
    second = handle_events_api_payload(ctx, mention_payload, bot_user_id="UBOT", http_post=fake_post)
    assert first is not None
    assert second is None
    assert openai_calls == 1


def test_advisor_bot_reply_does_not_reenter_chatgpt_bridge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    openai_calls = 0

    def fake_post(url, headers, body=None):
        nonlocal openai_calls
        if body is not None and isinstance(body, dict) and "input" in body:
            openai_calls += 1
        return _fake_openai_response("ok")

    payload = {
        "team_id": "T1",
        "event_id": "Ev-bot-reply",
        "event": {
            "type": "message",
            "channel": "G_PRIVATE",
            "channel_type": "group",
            "thread_ts": "100.0",
            "ts": "502.0",
            "user": "UBOT",
            "bot_id": "B1",
            "text": f"{CHATGPT_PREFIX}\nPending proposal still waiting.",
        },
    }
    assert handle_events_api_payload(ctx, payload, bot_user_id="UBOT", http_post=fake_post) is None
    assert openai_calls == 0


def test_new_sponsor_reply_with_pending_proposal_one_response(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        from projectos.chatgpt_store import upsert_chatgpt_thread

        upsert_chatgpt_thread(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="600.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            active=True,
            openai_response_id="resp_prior",
        )
        create_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="600.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            intent="work_preview",
            instruction="summary",
        )

    openai_calls = 0

    def fake_post(url, headers, body=None):
        nonlocal openai_calls
        if body is not None and isinstance(body, dict) and "input" in body:
            openai_calls += 1
            return _fake_openai_response("Follow-up answer.")
        return _fake_openai_response("unused")

    payload = _chatgpt_event_payload(
        event_id="Ev-followup",
        ts="601.0",
        text="Can you elaborate?",
        event_type="message",
    )
    payload["event"]["thread_ts"] = "600.0"
    reply = handle_events_api_payload(ctx, payload, bot_user_id="UBOT", http_post=fake_post)
    assert reply is not None
    assert openai_calls == 1


def test_read_only_summary_no_proposal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)

    def fake_post(url, headers, body):
        return _fake_openai_response("Concise summary ready.")

    reply = handle_chatgpt_slack_message(
        ctx,
        text=_mention("give me a concise summary of prj-003."),
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="800.0",
        message_ts="801.0",
        user_id="U1",
        http_post=fake_post,
    )
    assert reply is not None
    assert PROJECTOS_PREFIX in reply["text"]
    with connection(ctx.db_path) as conn:
        pending = list_pending_proposals(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="800.0",
            sponsor_user_id="U1",
        )
        assert pending == []


def test_thread_project_persists_without_prj_in_followup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)

    def fake_post(url, headers, body):
        return _fake_openai_response("Follow-up answer.")

    first = handle_chatgpt_slack_message(
        ctx,
        text=_mention("give me a concise summary of PRJ-003."),
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="810.0",
        message_ts="811.0",
        user_id="U1",
        http_post=fake_post,
    )
    assert first is not None
    second = handle_chatgpt_slack_message(
        ctx,
        text="What should I do next?",
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="810.0",
        message_ts="812.0",
        user_id="U1",
        http_post=fake_post,
    )
    assert second is not None
    with connection(ctx.db_path) as conn:
        from projectos.chatgpt_store import get_chatgpt_thread

        thread = get_chatgpt_thread(conn, team_id="T1", channel_id="G_PRIVATE", thread_ts="810.0")
        assert thread is not None
        assert thread["project_human_id"] == "PRJ-003"


def test_false_model_proposal_sent_claim_does_not_create_proposal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)

    def fake_post(url, headers, body):
        return _fake_openai_response(
            "Proposal sent to ProjectOS for PRJ-003 summary.\n```projectos_proposal\n"
            '{"intent":"summary","instruction":"summary"}\n```'
        )

    reply = handle_chatgpt_slack_message(
        ctx,
        text=_mention("review PRJ-003 architecture"),
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="820.0",
        message_ts="821.0",
        user_id="U1",
        http_post=fake_post,
    )
    assert reply is not None
    assert "proposal sent" not in reply["text"].lower()
    with connection(ctx.db_path) as conn:
        pending = list_pending_proposals(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="820.0",
            sponsor_user_id="U1",
        )
        assert pending == []


def test_approval_dispatches_exactly_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        from projectos.chatgpt_store import upsert_chatgpt_thread

        upsert_chatgpt_thread(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="700.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            active=True,
            openai_response_id="resp_prior",
        )
        record = create_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="700.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            intent="work_request",
            instruction="EXACT_APPROVAL",
        )
        _seed_preview(conn, proposal_id=record.proposal_id)

    openai_calls = 0
    submit_calls = {"n": 0}

    def fake_post(url, headers, body=None):
        nonlocal openai_calls
        if body is not None and isinstance(body, dict) and "input" in body:
            openai_calls += 1
            if openai_calls == 1:
                return _fake_openai_response("Approved and done.")
            return _fake_openai_response("ProjectOS created JOB-1 as expected.")
        return _fake_openai_response("unused")

    class FakeIntake:
        def submit(self, project_human_id, **kwargs):
            submit_calls["n"] += 1
            return IntakeResult(
                status="submitted",
                project_human_id=project_human_id,
                dry_run=False,
                jobs_created=["JOB-1"],
            )

    monkeypatch.setattr("projectos.slack_chatgpt.IntakeService", lambda ctx: FakeIntake())

    payload = _chatgpt_event_payload(
        event_id="Ev-approval",
        ts="701.0",
        text="Approved",
        event_type="message",
    )
    payload["event"]["thread_ts"] = "700.0"
    first = handle_events_api_payload(ctx, payload, bot_user_id="UBOT", http_post=fake_post)
    second = handle_events_api_payload(ctx, payload, bot_user_id="UBOT", http_post=fake_post)
    assert first is not None
    assert second is None
    assert openai_calls == 1
    assert submit_calls["n"] == 1
