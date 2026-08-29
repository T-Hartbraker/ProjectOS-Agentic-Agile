"""Slack token setup saves to encrypted local storage."""

from __future__ import annotations

import pytest

from projectos.errors import OrchestrationError
from projectos.secret_store import clear_slack_secrets, read_slack_secrets
from projectos.slack_token_setup import apply_slack_tokens
from projectos.slack_tokens import reload_slack_tokens


def test_apply_slack_tokens_validates_prefixes(tmp_path) -> None:
    secrets_path = tmp_path / "slack_secrets.enc"
    with pytest.raises(OrchestrationError, match="xapp-"):
        apply_slack_tokens(app_token="bad-token", secrets_path=secrets_path)
    with pytest.raises(OrchestrationError, match="xoxb-"):
        apply_slack_tokens(bot_token="bad-token", secrets_path=secrets_path)


def test_apply_slack_tokens_never_returns_secret_values(tmp_path, monkeypatch) -> None:
    secrets_path = tmp_path / "slack_secrets.enc"
    monkeypatch.setattr("projectos.secret_store._secrets_file", lambda: secrets_path)
    monkeypatch.delenv("PROJECTOS_SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("PROJECTOS_SLACK_BOT_TOKEN", raising=False)
    reload_slack_tokens()

    result = apply_slack_tokens(
        app_token="xapp-test-app",
        bot_token="xoxb-test-bot",
    )
    assert result["ok"] is True
    assert set(result["updated_fields"]) == {"app_token", "bot_token"}
    assert result["app_token"] == "xapp-...configured"
    assert result["bot_token"] == "xoxb-...configured"
    assert result["storage"] == "encrypted_local_store"
    assert "xapp-test-app" not in str(result)
    assert "xoxb-test-bot" not in str(result)
    stored = read_slack_secrets(secrets_path=secrets_path)
    assert stored["app_token"] == "xapp-test-app"
    assert stored["bot_token"] == "xoxb-test-bot"
    clear_slack_secrets(secrets_path=secrets_path)
    reload_slack_tokens()


def test_apply_slack_tokens_requires_at_least_one_value(tmp_path) -> None:
    with pytest.raises(OrchestrationError, match="at least one token"):
        apply_slack_tokens(secrets_path=tmp_path / "slack_secrets.enc")
