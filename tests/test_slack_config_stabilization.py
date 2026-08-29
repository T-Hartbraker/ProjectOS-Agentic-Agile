"""Slack credential resolver and configuration stabilization tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from projectos.errors import OrchestrationError
from projectos.secret_store import clear_slack_secrets, read_slack_secrets, write_slack_secrets
from projectos.slack_settings import read_slack_settings
from projectos.slack_state import public_connection, write_slack_state
from projectos.slack_token_setup import apply_slack_tokens, probe_slack_connection
from projectos.slack_tokens import reload_slack_tokens, resolve_slack_credentials, token_report


def _isolate(monkeypatch, tmp_path: Path) -> Path:
    secrets_path = tmp_path / "slack_secrets.enc"
    monkeypatch.setattr("projectos.secret_store._secrets_file", lambda: secrets_path)
    monkeypatch.delenv("PROJECTOS_SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("PROJECTOS_SLACK_BOT_TOKEN", raising=False)
    reload_slack_tokens()
    return secrets_path


def test_blank_token_save_preserves_existing_secret(tmp_path: Path, monkeypatch) -> None:
    secrets_path = _isolate(monkeypatch, tmp_path)
    apply_slack_tokens(
        app_token="xapp-keep",
        bot_token="xoxb-keep",
        secrets_path=secrets_path,
    )
    apply_slack_tokens(app_token="xapp-updated", secrets_path=secrets_path)
    stored = read_slack_secrets(secrets_path=secrets_path)
    assert stored["app_token"] == "xapp-updated"
    assert stored["bot_token"] == "xoxb-keep"


def test_environment_override_precedence(tmp_path: Path, monkeypatch) -> None:
    secrets_path = _isolate(monkeypatch, tmp_path)
    write_slack_secrets(
        {"app_token": "xapp-store", "bot_token": "xoxb-store"},
        secrets_path=secrets_path,
    )
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-env")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-env")
    reload_slack_tokens()
    creds = resolve_slack_credentials(refresh=True)
    assert creds["app_token"] == "xapp-env"
    assert creds["bot_token"] == "xoxb-env"
    assert creds["app_token_source"] == "environment"
    assert creds["bot_token_source"] == "environment"


def test_reload_after_restart_still_configured(tmp_path: Path, monkeypatch) -> None:
    secrets_path = _isolate(monkeypatch, tmp_path)
    apply_slack_tokens(
        app_token="xapp-restart",
        bot_token="xoxb-restart",
        secrets_path=secrets_path,
    )
    reload_slack_tokens()
    creds = resolve_slack_credentials(refresh=True)
    assert creds["configured"] is True
    assert creds["tokens_ready"] is True
    assert creds["storage"] == "encrypted_local_store"


def test_public_connection_does_not_report_not_configured_when_tokens_exist(
    tmp_path: Path, monkeypatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-live")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-live")
    reload_slack_tokens()
    state_path = tmp_path / "slack_socket.json"
    write_slack_state({"status": "not_configured"}, path=state_path)
    info = public_connection(enabled=True, tokens_ready=True, path=state_path)
    assert info["status"] != "not_configured"
    assert info["status"] == "disconnected"


def test_dashboard_settings_match_canonical_resolver(tmp_path: Path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-dash")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-dash")
    reload_slack_tokens()
    creds = resolve_slack_credentials(refresh=True)
    settings = read_slack_settings(db_path=tmp_path / "unused.db")
    assert settings["configured"] == creds["configured"]
    assert settings["app_token_configured"] == creds["app_token_present"]
    assert settings["bot_token_configured"] == creds["bot_token_present"]
    assert settings["app_token_source"] == creds["app_token_source"]
    assert settings["bot_token_source"] == creds["bot_token_source"]


def test_test_connection_uses_canonical_resolver(monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    reload_slack_tokens()

    def fake_post(url, headers, body=None):
        if "apps.connections.open" in url:
            return {"ok": True, "url": "wss://example"}
        if "auth.test" in url:
            return {"ok": True, "team": "Acme", "team_id": "T1", "user_id": "U1"}
        return {"ok": True}

    result = probe_slack_connection(http_post=fake_post)
    assert result["ok"] is True
    assert result["app_token"] == "PASS"
    assert result["bot_token"] == "PASS"
    assert result["socket_mode"] == "PASS"
    assert result["workspace"] == "Acme"


def test_test_connection_not_configured_error(monkeypatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(OrchestrationError, match="not configured"):
        probe_slack_connection()


def test_secrets_never_returned_by_token_report(tmp_path: Path, monkeypatch) -> None:
    secrets_path = _isolate(monkeypatch, tmp_path)
    apply_slack_tokens(
        app_token="xapp-secret-value",
        bot_token="xoxb-secret-value",
        secrets_path=secrets_path,
    )
    report = token_report(refresh=True)
    blob = str(report)
    assert "xapp-secret-value" not in blob
    assert "xoxb-secret-value" not in blob
    clear_slack_secrets(secrets_path=secrets_path)
    reload_slack_tokens()
