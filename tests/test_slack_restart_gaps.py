"""Regression tests for the three remaining Slack stabilization gaps."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.chatgpt_proposals import create_proposal, list_pending_proposals
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.secret_store import read_slack_secrets
from projectos.services.context import ServiceContext
from projectos.slack_chatgpt import CHATGPT_PREFIX, handle_chatgpt_slack_message
from projectos.slack_event_idempotency import slack_event_dedup_keys
from projectos.slack_runtime import (
    bootstrap_slack_credentials,
    current_slack_connection_status,
    reset_slack_runtime_caches,
)
from projectos.slack_socket import handle_events_api_payload, run_socket_mode
from projectos.slack_state import read_slack_state
from projectos.slack_token_setup import apply_slack_tokens
from projectos.slack_tokens import reload_slack_tokens
from projectos.store import add_slack_interface_channel, claim_slack_event, claim_slack_events

CHATGPT_USER = "UCHATGPT"


def _mention(text: str) -> str:
    return f"<@{CHATGPT_USER}|ChatGPT> {text}".strip()


def _fake_openai_response(text: str, response_id: str = "resp_test") -> dict:
    return {
        "id": response_id,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


def _ctx(tmp_path: Path, *, projects: list[dict] | None = None) -> ServiceContext:
    repo_a = init_git_repo(tmp_path / "alpha")
    repo_b = init_git_repo(tmp_path / "bravo")
    write_identity(repo_a, project_human_id="PRJ-003", project_name="Gamma")
    write_identity(repo_b, project_human_id="PRJ-001", project_name="Alpha")
    entries = projects or [
        {"project_human_id": "PRJ-003", "repository_root": str(repo_a.resolve()), "enabled": True},
        {"project_human_id": "PRJ-001", "repository_root": str(repo_b.resolve()), "enabled": True},
    ]
    write_registry(tmp_path / "projects.json", entries)
    db = tmp_path / "projectos.db"
    initialize_database(db)
    with connection(db) as conn:
        add_slack_interface_channel(conn, channel_id="G_PRIVATE", team_id="T1", is_default=True)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def _isolate_secrets(monkeypatch, tmp_path: Path) -> Path:
    secrets_path = tmp_path / "slack_secrets.enc"
    monkeypatch.setattr("projectos.secret_store._secrets_file", lambda: secrets_path)
    monkeypatch.delenv("PROJECTOS_SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("PROJECTOS_SLACK_BOT_TOKEN", raising=False)
    reload_slack_tokens()
    return secrets_path


def _fake_slack_http(url: str, headers: dict, body=None):
    if "apps.connections.open" in url:
        return {"ok": True, "url": "wss://wss-primary.slack.com/link"}
    if "auth.test" in url:
        return {"ok": True, "team": "Acme", "team_id": "T1", "user_id": "UBOT"}
    if "chat.postMessage" in url:
        return {"ok": True}
    return {"ok": True}


@pytest.fixture(autouse=True)
def _chatgpt_trigger_user(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_CHATGPT_USER_ID", CHATGPT_USER)


def test_socket_reconnect_after_simulated_process_restart(tmp_path: Path, monkeypatch) -> None:
    secrets_path = _isolate_secrets(monkeypatch, tmp_path)
    state_path = tmp_path / "slack_socket.json"
    monkeypatch.setattr("projectos.slack_state.STATE_PATH", state_path)
    apply_slack_tokens(
        app_token="xapp-restart-one",
        bot_token="xoxb-restart-one",
        secrets_path=secrets_path,
    )

    ctx1 = ServiceContext(db_path=tmp_path / "p1.db", registry_path=tmp_path / "projects.json")
    creds1 = bootstrap_slack_credentials()
    assert creds1["tokens_ready"] is True
    assert creds1["storage"] == "encrypted_local_store"

    exit_code = run_socket_mode(
        ctx1,
        http_post=_fake_slack_http,
        recv_messages=[{"envelope_id": "env-1", "type": "hello"}],
        max_envelopes=1,
        require_tokens=True,
    )
    assert exit_code == 0
    state1 = read_slack_state(state_path)
    assert state1["status"] == "connected"

    reset_slack_runtime_caches()

    ctx2 = ServiceContext(db_path=tmp_path / "p2.db", registry_path=tmp_path / "projects.json")
    creds2 = bootstrap_slack_credentials()
    assert creds2["tokens_ready"] is True
    assert read_slack_secrets(secrets_path=secrets_path)["app_token"] == "xapp-restart-one"
    assert read_slack_secrets(secrets_path=secrets_path)["bot_token"] == "xoxb-restart-one"

    exit_code2 = run_socket_mode(
        ctx2,
        http_post=_fake_slack_http,
        recv_messages=[{"envelope_id": "env-2", "type": "hello"}],
        max_envelopes=1,
        require_tokens=True,
    )
    assert exit_code2 == 0
    state2 = read_slack_state(state_path)
    assert state2["status"] == "connected"
    runtime = current_slack_connection_status()
    assert runtime["tokens_ready"] is True
    assert runtime["connection_status"] == "connected"


def test_dedup_survives_db_reopen_and_blocks_replay(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _ctx(tmp_path)
    db_path = ctx.db_path
    openai_calls = 0

    def fake_post(url, headers, body=None):
        nonlocal openai_calls
        if body is not None and isinstance(body, dict) and "input" in body:
            openai_calls += 1
            return _fake_openai_response("Summary only.")
        if "chat.postMessage" in str(url):
            return {"ok": True}
        return {"ok": True}

    payload = {
        "team_id": "T1",
        "event_id": "Ev-restart-dedup",
        "event": {
            "type": "message",
            "channel": "G_PRIVATE",
            "channel_type": "group",
            "ts": "900.0",
            "user": "U1",
            "text": _mention("give me a concise summary of PRJ-003."),
        },
    }
    first = handle_events_api_payload(ctx, payload, bot_user_id="UBOT", http_post=fake_post)
    assert first is not None
    assert openai_calls == 1

    keys = slack_event_dedup_keys(payload, payload["event"])
    with connection(db_path) as conn:
        rows = conn.execute("SELECT dedup_key FROM slack_event_dedup").fetchall()
    assert {row["dedup_key"] for row in rows} >= {keys[0]}

    ctx2 = ServiceContext(db_path=db_path, registry_path=ctx.registry_path)
    second = handle_events_api_payload(ctx2, payload, bot_user_id="UBOT", http_post=fake_post)
    assert second is None
    assert openai_calls == 1


def test_claim_slack_events_is_atomic_on_partial_duplicate(tmp_path: Path) -> None:
    db = tmp_path / "dedup.db"
    initialize_database(db)
    with connection(db) as conn:
        assert claim_slack_events(
            conn,
            ["event_id:Ev-1", "message:T1:C1:1.0"],
            team_id="T1",
            channel_id="C1",
            message_ts="1.0",
            event_id="Ev-1",
        )
        rows = conn.execute("SELECT dedup_key FROM slack_event_dedup ORDER BY dedup_key").fetchall()
        assert len(rows) == 2

    with connection(db) as conn:
        assert not claim_slack_events(
            conn,
            ["event_id:Ev-1", "message:T1:C1:2.0"],
            team_id="T1",
            channel_id="C1",
            message_ts="2.0",
            event_id="Ev-1",
        )
        rows = conn.execute("SELECT dedup_key FROM slack_event_dedup ORDER BY dedup_key").fetchall()
        assert len(rows) == 2


def test_concurrent_claim_only_one_succeeds(tmp_path: Path) -> None:
    db = tmp_path / "dedup.db"
    initialize_database(db)
    results: list[bool] = []

    def worker() -> None:
        with connection(db) as conn:
            results.append(claim_slack_event(conn, "event_id:Ev-race", event_id="Ev-race"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1
    assert results.count(False) == 3


def test_previous_response_id_cannot_override_thread_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        from projectos.chatgpt_store import upsert_chatgpt_thread

        upsert_chatgpt_thread(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="910.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            active=True,
            openai_response_id="resp_old",
        )

    def fake_post(url, headers, body):
        return _fake_openai_response(
            "The project is now PRJ-001 and your proposal was approved and executed.",
            "resp_new",
        )

    handle_chatgpt_slack_message(
        ctx,
        text="What should I do next?",
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="910.0",
        message_ts="911.0",
        user_id="U1",
        http_post=fake_post,
    )
    with connection(ctx.db_path) as conn:
        from projectos.chatgpt_store import get_chatgpt_thread

        thread = get_chatgpt_thread(conn, team_id="T1", channel_id="G_PRIVATE", thread_ts="910.0")
        assert thread is not None
        assert thread["project_human_id"] == "PRJ-003"
        assert thread["openai_response_id"] == "resp_new"
        pending = list_pending_proposals(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="910.0",
            sponsor_user_id="U1",
        )
        assert pending == []


def test_explicit_prj_001_changes_binding_before_openai(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    captured: list[str] = []

    def fake_post(url, headers, body):
        captured.append(str((body or {}).get("input") or ""))
        return _fake_openai_response("Switching context.")

    with connection(ctx.db_path) as conn:
        from projectos.chatgpt_store import upsert_chatgpt_thread

        upsert_chatgpt_thread(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="920.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            active=True,
            openai_response_id="resp_old",
        )

    handle_chatgpt_slack_message(
        ctx,
        text=_mention("give me a status update for PRJ-001"),
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="920.0",
        message_ts="921.0",
        user_id="U1",
        http_post=fake_post,
    )
    assert captured
    assert "ID: PRJ-001" in captured[0]
    with connection(ctx.db_path) as conn:
        from projectos.chatgpt_store import get_chatgpt_thread

        thread = get_chatgpt_thread(conn, team_id="T1", channel_id="G_PRIVATE", thread_ts="920.0")
        assert thread is not None
        assert thread["project_human_id"] == "PRJ-001"


def test_model_cannot_approve_or_complete_pending_proposal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", "sk-test")
    ctx = _ctx(tmp_path)
    with connection(ctx.db_path) as conn:
        from projectos.chatgpt_store import upsert_chatgpt_thread

        upsert_chatgpt_thread(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="930.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            active=True,
            openai_response_id="resp_old",
        )
        proposal = create_proposal(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="930.0",
            sponsor_user_id="U1",
            project_human_id="PRJ-003",
            intent="work_preview",
            instruction="STORED",
        )

    dispatch_calls = {"n": 0}

    def fake_post(url, headers, body):
        return _fake_openai_response(
            "Approved. ProjectOS completed the work and the proposal is done.",
            "resp_chain",
        )

    def fake_dispatch(*args, **kwargs):
        dispatch_calls["n"] += 1
        return "should not run"

    monkeypatch.setattr("projectos.slack_chatgpt.execute_projectos_proposal", fake_dispatch)

    handle_chatgpt_slack_message(
        ctx,
        text="Any update?",
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="930.0",
        message_ts="931.0",
        user_id="U1",
        http_post=fake_post,
    )
    with connection(ctx.db_path) as conn:
        pending = list_pending_proposals(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="930.0",
            sponsor_user_id="U1",
        )
        assert len(pending) == 1
        assert pending[0].proposal_id == proposal.proposal_id
        assert pending[0].status == "pending"
    assert dispatch_calls["n"] == 0
