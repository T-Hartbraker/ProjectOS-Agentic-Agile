"""Regression tests for private-channel ProjectOS thread follow-ups."""

from __future__ import annotations

from pathlib import Path

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.services.context import ServiceContext
from projectos.slack_event_routing import infer_channel_type, should_route_projectos_thread_followup
from projectos.slack_socket import handle_events_api_payload, process_socket_envelope
from projectos.slack_thread_context import is_projectos_thread_active, mark_projectos_thread_active
from projectos.store import add_slack_interface_channel, get_slack_project_context


def _ctx(tmp_path: Path) -> ServiceContext:
    repo_a = init_git_repo(tmp_path / "alpha")
    repo_b = init_git_repo(tmp_path / "bravo")
    write_identity(repo_a, project_human_id="PRJ-003", project_name="Personal Task Manager Pilot")
    write_identity(repo_b, project_human_id="PRJ-001", project_name="Phase 2 Isolation Pilot")
    write_registry(
        tmp_path / "projects.json",
        [
            {"project_human_id": "PRJ-003", "repository_root": str(repo_a.resolve()), "enabled": True},
            {"project_human_id": "PRJ-001", "repository_root": str(repo_b.resolve()), "enabled": True},
        ],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    with connection(db) as conn:
        add_slack_interface_channel(conn, channel_id="G_PRIVATE", team_id="T1", is_default=True)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def _mention_event(*, ts: str = "100.0", text: str = "<@UBOT> status") -> dict:
    return {
        "team_id": "T1",
        "event": {
            "type": "app_mention",
            "channel": "G_PRIVATE",
            "ts": ts,
            "user": "U1",
            "text": text,
        },
    }


def _thread_reply_event(*, thread_ts: str, ts: str, text: str) -> dict:
    return {
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": "G_PRIVATE",
            "ts": ts,
            "thread_ts": thread_ts,
            "user": "U1",
            "text": text,
        },
    }


def test_private_channel_inferred_without_channel_type() -> None:
    event = {"type": "message", "channel": "G_PRIVATE", "thread_ts": "100.0", "ts": "101.0"}
    assert infer_channel_type(event) == "group"


def test_app_mention_status_returns_clarification(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _ctx(tmp_path)
    reply = handle_events_api_payload(ctx, _mention_event(), bot_user_id="UBOT")
    assert reply is not None
    assert "Which ProjectOS project" in reply["text"]
    with connection(ctx.db_path) as conn:
        assert is_projectos_thread_active(
            conn, team_id="T1", channel_id="G_PRIVATE", thread_ts="100.0"
        )


def test_thread_reply_status_returns_clarification_not_silence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _ctx(tmp_path)
    first = handle_events_api_payload(ctx, _mention_event(), bot_user_id="UBOT")
    assert first is not None
    second = handle_events_api_payload(
        ctx,
        _thread_reply_event(thread_ts="100.0", ts="101.0", text="status"),
        bot_user_id="UBOT",
    )
    assert second is not None
    assert "Which ProjectOS project" in second["text"]


def test_thread_reply_project_id_binds_context(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _ctx(tmp_path)
    handle_events_api_payload(ctx, _mention_event(), bot_user_id="UBOT")
    bind = handle_events_api_payload(
        ctx,
        _thread_reply_event(thread_ts="100.0", ts="102.0", text="PRJ-003"),
        bot_user_id="UBOT",
    )
    assert bind is not None
    assert "Project context set to PRJ-003" in bind["text"]
    with connection(ctx.db_path) as conn:
        row = get_slack_project_context(
            conn, team_id="T1", channel_id="G_PRIVATE", thread_ts="100.0", user_id="U1"
        )
        assert row is not None
        assert row["project_human_id"] == "PRJ-003"


def test_subsequent_status_uses_bound_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _ctx(tmp_path)
    handle_events_api_payload(ctx, _mention_event(), bot_user_id="UBOT")
    handle_events_api_payload(
        ctx,
        _thread_reply_event(thread_ts="100.0", ts="102.0", text="PRJ-003"),
        bot_user_id="UBOT",
    )
    status = handle_events_api_payload(
        ctx,
        _thread_reply_event(thread_ts="100.0", ts="103.0", text="status"),
        bot_user_id="UBOT",
    )
    assert status is not None
    assert "Which ProjectOS project" not in status["text"]
    assert "PRJ-003" in status["text"] or "Personal Task Manager" in status["text"]


def test_unrelated_thread_message_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _ctx(tmp_path)
    handle_events_api_payload(ctx, _mention_event(ts="100.0"), bot_user_id="UBOT")
    unrelated = handle_events_api_payload(
        ctx,
        _thread_reply_event(thread_ts="999.0", ts="200.0", text="status"),
        bot_user_id="UBOT",
    )
    assert unrelated is None


def test_bot_authored_thread_reply_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        mark_projectos_thread_active(conn, team_id="T1", channel_id="G_PRIVATE", thread_ts="100.0")
    payload = {
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": "G_PRIVATE",
            "channel_type": "group",
            "thread_ts": "100.0",
            "ts": "104.0",
            "user": "UBOT",
            "text": "bot follow-up",
        },
    }
    assert handle_events_api_payload(ctx, payload, bot_user_id="UBOT") is None


def test_should_route_requires_active_projectos_thread_for_followup() -> None:
    event = {
        "type": "message",
        "channel": "G_PRIVATE",
        "thread_ts": "100.0",
        "ts": "101.0",
    }
    assert not should_route_projectos_thread_followup(
        event,
        projectos_thread_active=False,
        chatgpt_thread_active=False,
        text="status",
    )
    assert should_route_projectos_thread_followup(
        event,
        projectos_thread_active=True,
        chatgpt_thread_active=False,
        text="status",
    )


def test_socket_envelope_posts_thread_reply(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _ctx(tmp_path)
    posts: list[dict] = []

    def fake_post(url, headers, body=None):
        if body is None:
            body = {}
        if "chat.postMessage" in url:
            posts.append(body)
            return {"ok": True}
        return {"ok": True}

    process_socket_envelope(
        ctx,
        {
            "envelope_id": "env-thread-1",
            "type": "events_api",
            "payload": _mention_event(ts="100.0"),
        },
        http_post=fake_post,
        bot_user_id="UBOT",
    )
    process_socket_envelope(
        ctx,
        {
            "envelope_id": "env-thread-2",
            "type": "events_api",
            "payload": _thread_reply_event(thread_ts="100.0", ts="101.0", text="status"),
        },
        http_post=fake_post,
        bot_user_id="UBOT",
    )
    assert len(posts) == 2
    assert posts[1].get("thread_ts") == "100.0"
