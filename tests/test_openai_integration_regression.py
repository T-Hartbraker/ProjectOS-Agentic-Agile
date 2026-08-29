"""Regression tests for OpenAI settings + ChatGPT Slack bridge failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.secret_store import write_openai_secrets
from projectos.services.context import ServiceContext
from projectos.slack_chatgpt import (
    CHATGPT_PREFIX,
    PROJECTOS_PREFIX,
    is_chatgpt_addressed,
    try_handle_chatgpt_event,
)
from projectos.slack_event_routing import is_registered_interface_channel_event
from projectos.slack_socket import handle_events_api_payload
from projectos.openai_tokens import reload_openai_tokens
from projectos.store import add_slack_interface_channel, claim_slack_events, release_slack_events

CHATGPT_USER = "U0BTHBJK51A"
PLAINTEXT_KEY = "sk-test-openai-secret-key-value"


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
        add_slack_interface_channel(conn, channel_id="C_PUBLIC", team_id="T1", is_default=True)
    return ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")


def _mention(text: str) -> str:
    return f"<@{CHATGPT_USER}|ChatGPT> {text}".strip()


def _fake_openai_response(text: str, response_id: str = "resp_test") -> dict:
    return {
        "id": response_id,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


@pytest.fixture(autouse=True)
def _chatgpt_trigger_user(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_CHATGPT_USER_ID", CHATGPT_USER)


def test_registered_public_interface_channel_accepts_chatgpt_message(tmp_path: Path) -> None:
    event = {
        "type": "message",
        "channel": "C_PUBLIC",
        "channel_type": "channel",
        "ts": "100.0",
        "user": "U1",
        "text": _mention("status"),
    }
    assert is_registered_interface_channel_event(event, registered_channel_ids={"C_PUBLIC"})


def test_chatgpt_slack_mention_format_triggers_bridge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", PLAINTEXT_KEY)
    ctx = _ctx(tmp_path)
    text = _mention("give me a concise summary of PRJ-003.")
    assert is_chatgpt_addressed(text, event={"text": text})

    def fake_post(url, headers, body=None):
        if body is not None and isinstance(body, dict) and "input" in body:
            return _fake_openai_response("Summary only.")
        return {"ok": True}

    payload = {
        "team_id": "T1",
        "event_id": "Ev-chatgpt-public",
        "event": {
            "type": "message",
            "channel": "C_PUBLIC",
            "channel_type": "channel",
            "ts": "101.0",
            "user": "U1",
            "text": text,
        },
    }
    reply = handle_events_api_payload(ctx, payload, bot_user_id="UBOT", http_post=fake_post)
    assert reply is not None
    assert CHATGPT_PREFIX in reply["text"]


def test_unhandled_event_releases_dedup_claim_for_retry(tmp_path: Path) -> None:
    db = tmp_path / "dedup.db"
    initialize_database(db)
    keys = ["event_id:Ev-retry", "message:T1:C9:9.0"]
    with connection(db) as conn:
        assert claim_slack_events(conn, keys, team_id="T1", channel_id="C9", message_ts="9.0", event_id="Ev-retry")
        release_slack_events(conn, keys)
        assert claim_slack_events(conn, keys, team_id="T1", channel_id="C9", message_ts="9.0", event_id="Ev-retry")


def test_openai_unavailable_returns_projectos_failure_reply(tmp_path: Path, monkeypatch) -> None:
    from projectos.secret_store import PROJECTOS_SECRETS_FILE

    secrets_path = tmp_path / PROJECTOS_SECRETS_FILE
    monkeypatch.setattr("projectos.secret_store._projectos_secrets_file", lambda: secrets_path)
    monkeypatch.delenv("PROJECTOS_OPENAI_API_KEY", raising=False)
    reload_openai_tokens()
    ctx = _ctx(tmp_path)
    reply = try_handle_chatgpt_event(
        ctx,
        event={
            "type": "message",
            "channel": "C_PUBLIC",
            "channel_type": "channel",
            "ts": "102.0",
            "user": "U1",
            "text": _mention("summary"),
        },
        payload={"team_id": "T1"},
        registered_channel_ids={"C_PUBLIC"},
    )
    assert reply is not None
    assert PROJECTOS_PREFIX in reply["text"]
    assert "unavailable" in reply["text"].lower()


def test_openai_settings_api_includes_trigger_user(tmp_path: Path, monkeypatch) -> None:
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from projectos.http import create_app
    from projectos.secret_store import PROJECTOS_SECRETS_FILE

    secrets_path = tmp_path / PROJECTOS_SECRETS_FILE
    monkeypatch.setattr("projectos.secret_store._projectos_secrets_file", lambda: secrets_path)
    monkeypatch.setattr("projectos.paths.STATE_DIR", tmp_path / "state")
    monkeypatch.setattr("projectos.openai_config.STATE_DIR", tmp_path / "state")
    monkeypatch.setattr("projectos.openai_config._CONFIG_PATH", tmp_path / "state" / "openai_config.json")
    monkeypatch.setattr("projectos.openai_state.STATE_DIR", tmp_path / "state")
    monkeypatch.setattr("projectos.openai_state._STATE_PATH", tmp_path / "state" / "openai_state.json")
    monkeypatch.delenv("PROJECTOS_OPENAI_API_KEY", raising=False)
    write_openai_secrets({"api_key": PLAINTEXT_KEY}, secrets_path=secrets_path)
    reload_openai_tokens()

    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-A", project_name="Alpha")
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-A", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    client = TestClient(
        create_app(
            registry_path=tmp_path / "projects.json",
            db_path=tmp_path / "projectos.db",
            skip_identity_validation=True,
        )
    )
    initialize_database(tmp_path / "projectos.db")
    response = client.get("/v1/settings/integrations/openai")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_key_configured"] is True
    assert body["slack_chatgpt_user_id"] == CHATGPT_USER
    assert PLAINTEXT_KEY not in response.text
