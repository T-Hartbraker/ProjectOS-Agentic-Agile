"""Slack Socket Mode is the local default. Tests never call a live workspace."""

from __future__ import annotations

from pathlib import Path

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.services.context import ServiceContext
from projectos.services.facades import SlackBindingService
from projectos.slack_replies import UNAUTHORIZED_CHANNEL_TEXT
from projectos.slack_socket import (
    ack_envelope,
    handle_events_api_payload,
    handle_projectos_request,
    handle_slash_commands_payload,
    open_socket_url,
    process_socket_envelope,
    run_socket_mode,
)
from projectos.slack_slash import project_override_attempt
from projectos.slack_status import public_slack_status
from projectos.slack_tokens import contains_secret, token_report
from projectos.store import add_slack_interface_channel, get_slack_project_context


def _ctx(tmp_path: Path, *, projects: list[dict] | None = None) -> ServiceContext:
    repo_a = init_git_repo(tmp_path / "alpha")
    repo_b = init_git_repo(tmp_path / "bravo")
    write_identity(repo_a, project_human_id="PRJ-A", project_name="Alpha")
    write_identity(repo_b, project_human_id="PRJ-B", project_name="Bravo")
    entries = projects or [
        {"project_human_id": "PRJ-A", "repository_root": str(repo_a.resolve()), "enabled": True},
        {"project_human_id": "PRJ-B", "repository_root": str(repo_b.resolve()), "enabled": True},
    ]
    write_registry(tmp_path / "projects.json", entries)
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def _slash_envelope(envelope_id: str, text: str, *, channel: str = "C1", team: str = "T1", user: str = "U1") -> dict:
    return {
        "envelope_id": envelope_id,
        "type": "slash_commands",
        "payload": {
            "command": "/projectos",
            "text": text,
            "channel_id": channel,
            "team_id": team,
            "user_id": user,
            "response_url": "https://hooks.slack.com/commands/test",
        },
    }


def _add_interface(ctx: ServiceContext, channel: str, team: str = "T1") -> None:
    with connection(ctx.db_path) as conn:
        add_slack_interface_channel(conn, channel_id=channel, team_id=team, is_default=True)


def _isolate_slack_tokens(monkeypatch, tmp_path: Path | None = None) -> None:
    from projectos.slack_tokens import reload_slack_tokens

    if tmp_path is not None:
        secrets_path = tmp_path / "slack_secrets.enc"
        monkeypatch.setattr("projectos.secret_store._secrets_file", lambda: secrets_path)
    reload_slack_tokens()


def test_missing_tokens_are_reported_without_secrets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PROJECTOS_SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("PROJECTOS_SLACK_BOT_TOKEN", raising=False)
    _isolate_slack_tokens(monkeypatch, tmp_path)
    report = token_report()
    assert report["app_token"] == "missing"
    assert report["bot_token"] == "missing"
    assert contains_secret("no secrets here") is False


def test_token_prefix_validation(monkeypatch, tmp_path: Path) -> None:
    _isolate_slack_tokens(monkeypatch, tmp_path)
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "bad-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-good")
    report = token_report()
    assert report["app_token_valid_prefix"] is False
    assert report["bot_token_valid_prefix"] is True


def test_open_socket_url_uses_apps_connections_open(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    calls: list[str] = []

    def http_post(url: str, headers: dict, body: dict | None) -> dict:
        calls.append(url)
        assert "xapp-secret-app" not in str(body)
        return {"ok": True, "url": "wss://wss-primary.slack.com/link"}

    url = open_socket_url(http_post=http_post)
    assert url.startswith("wss://")
    assert "apps.connections.open" in calls[0]


def test_missing_app_token_run_is_not_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PROJECTOS_SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("PROJECTOS_SLACK_BOT_TOKEN", raising=False)
    _isolate_slack_tokens(monkeypatch, tmp_path)
    ctx = _ctx(tmp_path)
    assert run_socket_mode(ctx, recv_messages=[], require_tokens=True) == 0
    status = public_slack_status(db_path=ctx.db_path, slack_enabled=True)
    assert status["connection_status"] == "not_configured"
    assert "xapp-secret" not in str(status)
    assert "xoxb-secret" not in str(status)


def test_ack_envelope_and_duplicate_handling(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path)
    _add_interface(ctx, "C1")
    posts: list[dict] = []

    def http_post(url: str, headers: dict, body: dict | None) -> dict:
        posts.append({"url": url, "body": body})
        return {"ok": True}

    envelope = _slash_envelope("env-1", "use PRJ-A")
    first = process_socket_envelope(ctx, envelope, http_post=http_post)
    second = process_socket_envelope(ctx, envelope, http_post=http_post)
    assert first["ack"] == ack_envelope("env-1")
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert first["reply"]["text"]
    assert "xapp-secret-app" not in str(first)
    assert "xoxb-secret-bot" not in str(posts)


def test_unauthorized_channel_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path)
    reply = handle_slash_commands_payload(
        ctx,
        {
            "command": "/projectos",
            "text": "status",
            "channel_id": "CUNBOUND",
            "team_id": "T1",
            "user_id": "U1",
        },
    )
    assert reply["text"] == UNAUTHORIZED_CHANNEL_TEXT


def test_global_interface_channel_accepts_without_binding(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path, projects=[
        {"project_human_id": "PRJ-A", "repository_root": str((tmp_path / "alpha").resolve()), "enabled": True},
    ])
    _add_interface(ctx, "CGLOBAL")
    clarify = handle_slash_commands_payload(
        ctx,
        {"command": "/projectos", "text": "status", "channel_id": "CGLOBAL", "team_id": "T1", "user_id": "U1"},
    )
    assert "PRJ-A" in clarify["text"]


def test_use_establishes_context_and_status_uses_it(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path)
    _add_interface(ctx, "C1")
    use_reply = handle_slash_commands_payload(
        ctx,
        {"command": "/projectos", "text": "use PRJ-A", "channel_id": "C1", "team_id": "T1", "user_id": "U1"},
    )
    assert "PRJ-A" in use_reply["text"]
    with connection(ctx.db_path) as conn:
        row = get_slack_project_context(conn, team_id="T1", channel_id="C1", thread_ts="", user_id="U1")
        assert row is not None
        assert row["project_human_id"] == "PRJ-A"
    status = handle_slash_commands_payload(
        ctx,
        {"command": "/projectos", "text": "status", "channel_id": "C1", "team_id": "T1", "user_id": "U1"},
    )
    assert "PRJ-A" in status["text"]
    assert "Status:" in status["text"]


def test_context_isolated_between_users(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path)
    _add_interface(ctx, "C1")
    handle_slash_commands_payload(
        ctx,
        {"command": "/projectos", "text": "use PRJ-A", "channel_id": "C1", "team_id": "T1", "user_id": "U1"},
    )
    clarify = handle_slash_commands_payload(
        ctx,
        {"command": "/projectos", "text": "status", "channel_id": "C1", "team_id": "T1", "user_id": "U2"},
    )
    assert "Which ProjectOS project" in clarify["text"]


def test_explicit_project_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path)
    _add_interface(ctx, "C1")
    reply = handle_slash_commands_payload(
        ctx,
        {"command": "/projectos", "text": "PRJ-B status", "channel_id": "C1", "team_id": "T1", "user_id": "U1"},
    )
    assert "PRJ-B" in reply["text"]


def test_unknown_project_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path)
    _add_interface(ctx, "C1")
    reply = handle_slash_commands_payload(
        ctx,
        {"command": "/projectos", "text": "PRJ-ZZZ status", "channel_id": "C1", "team_id": "T1", "user_id": "U1"},
    )
    assert "Unknown project" in reply["text"]


def test_projects_lists_projects(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path)
    _add_interface(ctx, "C1")
    reply = handle_slash_commands_payload(
        ctx,
        {"command": "/projectos", "text": "projects", "channel_id": "C1", "team_id": "T1", "user_id": "U1"},
    )
    assert "PRJ-A" in reply["text"]
    assert "PRJ-B" in reply["text"]


def test_legacy_binding_still_works(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path)
    SlackBindingService(ctx).bind("PRJ-A", channel_id="CLEG", team_id="T1")
    reply = handle_slash_commands_payload(
        ctx,
        {"command": "/projectos", "text": "status", "channel_id": "CLEG", "team_id": "T1", "user_id": "U1"},
    )
    assert "PRJ-A" in reply["text"]


def test_app_mention_event_parsing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path, projects=[
        {"project_human_id": "PRJ-A", "repository_root": str((tmp_path / "alpha").resolve()), "enabled": True},
    ])
    _add_interface(ctx, "C1")
    reply = handle_events_api_payload(
        ctx,
        {
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "user": "U1",
                "text": "<@B1> status",
                "channel": "C1",
                "ts": "1.0",
            },
        },
    )
    assert reply is not None
    assert "PRJ-A" in reply["text"]


def test_message_im_event_parsing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path, projects=[
        {"project_human_id": "PRJ-A", "repository_root": str((tmp_path / "alpha").resolve()), "enabled": True},
    ])
    reply = handle_events_api_payload(
        ctx,
        {
            "team_id": "T1",
            "event": {
                "type": "message",
                "channel_type": "im",
                "user": "U1",
                "text": "status",
                "channel": "D123",
                "ts": "1.0",
            },
        },
    )
    assert reply is not None
    assert "PRJ-A" in reply["text"]


def test_bot_own_messages_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path)
    reply = handle_events_api_payload(
        ctx,
        {
            "team_id": "T1",
            "event": {
                "type": "message",
                "channel_type": "im",
                "user": "B1",
                "bot_id": "B1",
                "text": "status",
                "channel": "D123",
                "ts": "1.0",
            },
        },
        bot_user_id="B1",
    )
    assert reply is None


def test_malformed_event_rejected_safely(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path)
    reply = handle_events_api_payload(ctx, {"event": {"type": "app_mention"}})
    assert reply is None


def test_unknown_command_returns_help(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    ctx = _ctx(tmp_path)
    _add_interface(ctx, "C1")
    handle_slash_commands_payload(
        ctx,
        {"command": "/projectos", "text": "use PRJ-A", "channel_id": "C1", "team_id": "T1", "user_id": "U1"},
    )
    reply = handle_slash_commands_payload(
        ctx,
        {"command": "/projectos", "text": "nonsense", "channel_id": "C1", "team_id": "T1", "user_id": "U1"},
    )
    assert "Unknown ProjectOS command" in reply["text"]


def test_deprecated_flag_override_hint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    assert project_override_attempt("status --project PRJ-003") is True
    ctx = _ctx(tmp_path)
    _add_interface(ctx, "C1")
    reply = handle_projectos_request(
        ctx,
        text="status --project PRJ-003",
        channel_id="C1",
        team_id="T1",
        thread_ts=None,
        user_id="U1",
    )
    assert "prj-003" in reply["text"].lower()
