"""OpenAI encrypted secret storage and settings API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.http import create_app
from projectos.migrate import initialize_database
from projectos.openai_config import stored_openai_model
from projectos.openai_tokens import api_key, api_key_source, reload_openai_tokens
from projectos.secret_store import (
    OPENAI_API_KEY_ID,
    PROJECTOS_SECRETS_FILE,
    clear_openai_secrets,
    read_openai_secrets,
    read_projectos_secrets,
    write_openai_secrets,
)

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

PLAINTEXT_KEY = "sk-test-openai-secret-key-value"
OTHER_KEY = "sk-other-openai-key-for-override"


def _isolate_openai(tmp_path: Path, monkeypatch) -> Path:
    secrets_path = tmp_path / PROJECTOS_SECRETS_FILE
    monkeypatch.setattr("projectos.secret_store._projectos_secrets_file", lambda: secrets_path)
    monkeypatch.setattr("projectos.paths.STATE_DIR", tmp_path / "state")
    monkeypatch.setattr("projectos.openai_config.STATE_DIR", tmp_path / "state")
    monkeypatch.setattr("projectos.openai_config._CONFIG_PATH", tmp_path / "state" / "openai_config.json")
    monkeypatch.setattr("projectos.openai_state.STATE_DIR", tmp_path / "state")
    monkeypatch.setattr("projectos.openai_state._STATE_PATH", tmp_path / "state" / "openai_state.json")
    monkeypatch.delenv("PROJECTOS_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PROJECTOS_OPENAI_MODEL", raising=False)
    reload_openai_tokens()
    return secrets_path


def _client(tmp_path: Path) -> TestClient:
    alpha = init_git_repo(tmp_path / "alpha")
    write_identity(alpha, project_human_id="PRJ-A", project_name="Alpha")
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-A", "repository_root": str(alpha.resolve()), "enabled": True}],
    )
    return TestClient(
        create_app(
            registry_path=tmp_path / "projects.json",
            db_path=tmp_path / "projectos.db",
            projectctl_runner=lambda root: fake_status("PRJ-A"),
            skip_identity_validation=True,
        )
    )


def test_openai_secret_encrypts_at_rest(tmp_path: Path, monkeypatch) -> None:
    secrets_path = _isolate_openai(tmp_path, monkeypatch)
    write_openai_secrets({"api_key": PLAINTEXT_KEY}, secrets_path=secrets_path)
    raw = secrets_path.read_bytes()
    assert PLAINTEXT_KEY.encode() not in raw
    loaded = read_openai_secrets(secrets_path=secrets_path)
    assert loaded["api_key"] == PLAINTEXT_KEY
    unified = read_projectos_secrets(secrets_path=secrets_path)
    assert unified[OPENAI_API_KEY_ID] == PLAINTEXT_KEY


def test_openai_plaintext_not_in_sqlite_or_config(tmp_path: Path, monkeypatch) -> None:
    secrets_path = _isolate_openai(tmp_path, monkeypatch)
    db_path = tmp_path / "projectos.db"
    initialize_database(db_path)
    write_openai_secrets({"api_key": PLAINTEXT_KEY}, secrets_path=secrets_path)
    db_text = db_path.read_bytes()
    assert PLAINTEXT_KEY.encode() not in db_text
    projects = tmp_path / "projects.json"
    projects.write_text("{}", encoding="utf-8")
    assert PLAINTEXT_KEY not in projects.read_text(encoding="utf-8")


def test_openai_get_settings_never_returns_key(tmp_path: Path, monkeypatch) -> None:
    _isolate_openai(tmp_path, monkeypatch)
    write_openai_secrets({"api_key": PLAINTEXT_KEY})
    reload_openai_tokens()
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    response = client.get("/v1/settings/integrations/openai")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_key_configured"] is True
    assert body["api_key_source"] == "encrypted_store"
    assert PLAINTEXT_KEY not in response.text
    assert "sk-" not in response.text or "gpt-" in response.text


def test_openai_secret_api_save_delete_and_persist(tmp_path: Path, monkeypatch) -> None:
    secrets_path = _isolate_openai(tmp_path, monkeypatch)
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")

    saved = client.put("/v1/settings/integrations/openai/secret", json={"api_key": PLAINTEXT_KEY})
    assert saved.status_code == 200, saved.text
    assert saved.json()["api_key_configured"] is True
    assert PLAINTEXT_KEY not in saved.text
    reload_openai_tokens()
    assert api_key() == PLAINTEXT_KEY

    read_after = client.get("/v1/settings/integrations/openai")
    assert read_after.json()["api_key_configured"] is True

    deleted = client.delete("/v1/settings/integrations/openai/secret")
    assert deleted.status_code == 200, deleted.text
    reload_openai_tokens()
    assert api_key() == ""
    assert not secrets_path.is_file() or OPENAI_API_KEY_ID not in read_projectos_secrets(secrets_path=secrets_path)

    write_openai_secrets({"api_key": PLAINTEXT_KEY}, secrets_path=secrets_path)
    reload_openai_tokens()
    assert api_key() == PLAINTEXT_KEY


def test_openai_empty_secret_put_does_not_erase_existing(tmp_path: Path, monkeypatch) -> None:
    _isolate_openai(tmp_path, monkeypatch)
    write_openai_secrets({"api_key": PLAINTEXT_KEY})
    reload_openai_tokens()
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    response = client.put("/v1/settings/integrations/openai/secret", json={"api_key": ""})
    assert response.status_code != 200
    reload_openai_tokens()
    assert api_key() == PLAINTEXT_KEY


def test_openai_environment_overrides_encrypted_store(tmp_path: Path, monkeypatch) -> None:
    _isolate_openai(tmp_path, monkeypatch)
    write_openai_secrets({"api_key": PLAINTEXT_KEY})
    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", OTHER_KEY)
    reload_openai_tokens()
    assert api_key() == OTHER_KEY
    assert api_key_source() == "environment"
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    response = client.get("/v1/settings/integrations/openai")
    assert response.json()["api_key_source"] == "environment"


def test_openai_model_persists_in_config_not_secret_store(tmp_path: Path, monkeypatch) -> None:
    _isolate_openai(tmp_path, monkeypatch)
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    updated = client.put("/v1/settings/integrations/openai", json={"model": "gpt-4o-mini"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["model"] == "gpt-4o-mini"
    assert stored_openai_model() == "gpt-4o-mini"
    config_path = tmp_path / "state" / "openai_config.json"
    assert config_path.is_file()
    config_text = config_path.read_text(encoding="utf-8")
    assert PLAINTEXT_KEY not in config_text
    assert json.loads(config_text)["model"] == "gpt-4o-mini"


def test_openai_test_connection_uses_resolved_key(tmp_path: Path, monkeypatch) -> None:
    _isolate_openai(tmp_path, monkeypatch)
    write_openai_secrets({"api_key": PLAINTEXT_KEY})
    reload_openai_tokens()
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")

    captured: dict[str, str] = {}

    def fake_probe(*, http_post=None):
        captured["key"] = api_key()
        return {"ok": True, "response_id": "resp_test", "text": "ok"}

    monkeypatch.setattr("projectos.openai_secret_setup.probe_api", fake_probe)
    response = client.post("/v1/settings/integrations/openai/test")
    assert response.status_code == 200, response.text
    assert captured["key"] == PLAINTEXT_KEY
    assert response.json()["ok"] is True
    assert PLAINTEXT_KEY not in response.text


def test_openai_doctor_reports_source(tmp_path: Path, monkeypatch, capsys) -> None:
    from projectos.cli import cmd_openai_doctor

    _isolate_openai(tmp_path, monkeypatch)
    write_openai_secrets({"api_key": PLAINTEXT_KEY})
    reload_openai_tokens()
    args = type("Args", (), {"probe": False})()
    assert cmd_openai_doctor(args) == 0
    out = capsys.readouterr().out
    assert "configured: yes" in out
    assert "source: encrypted_store" in out
    assert "connection: not tested" in out or "connection: success" in out
    assert PLAINTEXT_KEY not in out

    monkeypatch.setenv("PROJECTOS_OPENAI_API_KEY", OTHER_KEY)
    reload_openai_tokens()
    assert cmd_openai_doctor(args) == 0
    out = capsys.readouterr().out
    assert "source: environment" in out


def test_openai_api_errors_do_not_leak_key(tmp_path: Path, monkeypatch) -> None:
    _isolate_openai(tmp_path, monkeypatch)
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    response = client.put("/v1/settings/integrations/openai/secret", json={"api_key": "not-a-valid-key"})
    assert response.status_code != 200
    assert PLAINTEXT_KEY not in response.text


def test_clear_openai_secrets_removes_key(tmp_path: Path, monkeypatch) -> None:
    secrets_path = _isolate_openai(tmp_path, monkeypatch)
    write_openai_secrets({"api_key": PLAINTEXT_KEY}, secrets_path=secrets_path)
    clear_openai_secrets(secrets_path=secrets_path)
    reload_openai_tokens()
    assert read_openai_secrets(secrets_path=secrets_path) == {}
    assert api_key() == ""
