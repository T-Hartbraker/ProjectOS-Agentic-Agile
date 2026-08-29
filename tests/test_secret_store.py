"""Encrypted local secret store."""

from __future__ import annotations

from pathlib import Path

from projectos.secret_store import clear_slack_secrets, read_slack_secrets, write_slack_secrets
from projectos.slack_tokens import reload_slack_tokens


def test_slack_secrets_roundtrip(tmp_path: Path) -> None:
    secrets_path = tmp_path / "slack_secrets.enc"
    write_slack_secrets(
        {"app_token": "xapp-roundtrip", "bot_token": "xoxb-roundtrip"},
        secrets_path=secrets_path,
    )
    loaded = read_slack_secrets(secrets_path=secrets_path)
    assert loaded["app_token"] == "xapp-roundtrip"
    assert loaded["bot_token"] == "xoxb-roundtrip"


def test_slack_secrets_merge_partial_updates(tmp_path: Path) -> None:
    secrets_path = tmp_path / "slack_secrets.enc"
    write_slack_secrets({"app_token": "xapp-one"}, secrets_path=secrets_path)
    write_slack_secrets({"bot_token": "xoxb-two"}, secrets_path=secrets_path)
    loaded = read_slack_secrets(secrets_path=secrets_path)
    assert loaded == {"app_token": "xapp-one", "bot_token": "xoxb-two"}


def test_slack_tokens_read_from_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PROJECTOS_SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("PROJECTOS_SLACK_BOT_TOKEN", raising=False)
    secrets_path = tmp_path / "slack_secrets.enc"
    write_slack_secrets(
        {"app_token": "xapp-store", "bot_token": "xoxb-store"},
        secrets_path=secrets_path,
    )
    monkeypatch.setattr("projectos.secret_store._secrets_file", lambda: secrets_path)
    reload_slack_tokens()
    from projectos.slack_tokens import app_token, bot_token, token_report

    assert app_token() == "xapp-store"
    assert bot_token() == "xoxb-store"
    report = token_report()
    assert report["app_token_present"] is True
    assert report["bot_token_present"] is True
    assert report["storage"] == "encrypted_local_store"
    clear_slack_secrets(secrets_path=secrets_path)
    reload_slack_tokens()
