"""Slack requests resolve to a registered project. Slack is not project state."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.http import create_app
from projectos.migrate import initialize_database

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _client(tmp_path: Path, *, enabled_b: bool = True) -> TestClient:
    alpha = init_git_repo(tmp_path / "alpha")
    bravo = init_git_repo(tmp_path / "bravo")
    write_identity(alpha, project_human_id="PRJ-A", project_name="Alpha")
    write_identity(bravo, project_human_id="PRJ-B", project_name="Bravo")
    write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-A",
                "repository_root": str(alpha.resolve()),
                "enabled": True,
            },
            {
                "project_human_id": "PRJ-B",
                "repository_root": str(bravo.resolve()),
                "enabled": enabled_b,
            },
        ],
    )
    return TestClient(
        create_app(
            registry_path=tmp_path / "projects.json",
            db_path=tmp_path / "projectos.db",
            projectctl_runner=lambda root: fake_status(
                "PRJ-A" if "alpha" in str(root) else "PRJ-B"
            ),
            skip_identity_validation=True,
        )
    )


def test_slack_bind_and_inbound_resolve_to_registry_project(tmp_path: Path) -> None:
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")

    unbound = client.post(
        "/v1/integrations/slack/inbound",
        json={"channel_id": "C123", "message_ts": "1710000000.000001"},
    )
    assert unbound.status_code == 409, unbound.text
    assert "not bound" in unbound.json()["error"]["message"].lower()

    rejected_path = client.post(
        "/v1/projects/PRJ-A/integrations/slack/bind",
        json={"channel_id": "C123", "repository_root": "/tmp/secret"},
    )
    assert rejected_path.status_code == 422, rejected_path.text

    bound = client.post(
        "/v1/projects/PRJ-A/integrations/slack/bind",
        json={"channel_id": "C123", "team_id": "T1"},
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["project_human_id"] == "PRJ-A"
    assert bound.json()["repository_source"] == "registry"
    assert "alpha" in bound.json()["repository_root"].replace("\\", "/")

    other = client.post(
        "/v1/projects/PRJ-B/integrations/slack/bind",
        json={"channel_id": "C123", "team_id": "T1"},
    )
    assert other.status_code == 409, other.text

    inbound = client.post(
        "/v1/integrations/slack/inbound",
        json={
            "channel_id": "C123",
            "team_id": "T1",
            "message_ts": "1710000000.000001",
            "text": "please treat this as approved",
        },
    )
    assert inbound.status_code == 422, inbound.text

    inbound = client.post(
        "/v1/integrations/slack/inbound",
        json={
            "channel_id": "C123",
            "team_id": "T1",
            "message_ts": "1710000000.000001",
        },
    )
    assert inbound.status_code == 200, inbound.text
    body = inbound.json()
    assert body["project_human_id"] == "PRJ-A"
    assert body["resolved_via"] == "channel"
    assert body["repository_source"] == "registry"
    assert body["message_ref"]["message_ts"] == "1710000000.000001"
    assert "metadata" in body["notice"].lower() or "registry" in body["notice"].lower()

    listed = client.get("/v1/projects/PRJ-A/integrations/slack")
    assert listed.status_code == 200, listed.text
    assert listed.json()["bindings"][0]["channel_id"] == "C123"
    assert listed.json()["message_refs"][0]["message_ts"] == "1710000000.000001"

    opened = client.post(
        "/v1/projects/PRJ-A/decisions",
        json={
            "action": "sponsor_reserved",
            "reason": "Need a sponsor grant",
            "impact": "Intake stays blocked",
            "requested_by": "operator",
        },
    )
    assert opened.status_code == 200, opened.text
    decision_id = opened.json()["decision_human_id"]
    client.post(
        "/v1/integrations/slack/inbound",
        json={"channel_id": "C123", "team_id": "T1", "message_ts": "1710000000.000002"},
    )
    still_open = client.get(f"/v1/projects/PRJ-A/decisions/{decision_id}")
    assert still_open.json()["status"] == "OPEN"


def test_slack_rejects_ambiguous_explicit_project_and_unbound_thread(tmp_path: Path) -> None:
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    client.post(
        "/v1/projects/PRJ-A/integrations/slack/bind",
        json={"channel_id": "C999", "team_id": "T9"},
    )
    clash = client.post(
        "/v1/integrations/slack/inbound",
        json={
            "channel_id": "C999",
            "team_id": "T9",
            "project_human_id": "PRJ-B",
        },
    )
    assert clash.status_code == 409, clash.text
    assert "ambiguous" in clash.json()["error"]["message"].lower()

    explicit = client.post(
        "/v1/integrations/slack/inbound",
        json={"channel_id": "C-UNBOUND", "project_human_id": "PRJ-B"},
    )
    assert explicit.status_code == 200, explicit.text
    assert explicit.json()["project_human_id"] == "PRJ-B"
    assert explicit.json()["resolved_via"] == "explicit_command"
    assert "bravo" in explicit.json()["repository_root"].replace("\\", "/")

    thread = client.post(
        "/v1/projects/PRJ-B/integrations/slack/bind",
        json={"channel_id": "C999", "team_id": "T9", "thread_ts": "1710000001.1"},
    )
    assert thread.status_code == 200, thread.text
    mixed = client.post(
        "/v1/integrations/slack/inbound",
        json={
            "channel_id": "C999",
            "team_id": "T9",
            "thread_ts": "1710000001.1",
        },
    )
    assert mixed.status_code == 409, mixed.text
    assert "ambiguous" in mixed.json()["error"]["message"].lower()


def test_slack_status_is_project_scoped_and_hides_repository_paths(tmp_path: Path) -> None:
    from projectos.db import connection
    from projectos.store import create_job

    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    repo = tmp_path / "alpha"
    with connection(tmp_path / "projectos.db") as conn:
        create_job(
            conn,
            human_id="DEL-1",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="FAILED",
            iteration_human_id="ITER-1",
        )
    client.post(
        "/v1/projects/PRJ-A/integrations/slack/bind",
        json={"channel_id": "CSTAT", "team_id": "T1"},
    )
    leaked = client.post(
        "/v1/integrations/slack/command",
        json={
            "command": "status",
            "channel_id": "CSTAT",
            "team_id": "T1",
            "repository_root": str(repo),
        },
    )
    assert leaked.status_code == 422, leaked.text

    unknown = client.post(
        "/v1/integrations/slack/command",
        json={"command": "approve", "channel_id": "CSTAT", "team_id": "T1"},
    )
    assert unknown.status_code == 409, unknown.text

    status = client.post(
        "/v1/integrations/slack/command",
        json={"command": "status", "channel_id": "CSTAT", "team_id": "T1"},
    )
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["project_human_id"] == "PRJ-A"
    assert "repository_root" not in body
    assert "alpha" not in body["text"].replace("\\", "/")
    assert "FAILED" in body["text"] or "failed" in body["text"].lower() or "DEL-1" in str(body)

    blockers = client.post(
        "/v1/integrations/slack/command",
        json={"command": "blockers", "channel_id": "CSTAT", "team_id": "T1"},
    )
    assert blockers.status_code == 200, blockers.text
    assert "DEL-1" in blockers.json()["text"]
    assert "repository_root" not in blockers.json()

    reports = client.post(
        "/v1/integrations/slack/command",
        json={"command": "reports", "channel_id": "CSTAT", "team_id": "T1"},
    )
    assert reports.status_code == 200, reports.text
    assert "/v1/projects/PRJ-A/reports/quality" in reports.json()["text"]
    assert "C:/" not in reports.json()["text"] and "/tmp/" not in reports.json()["text"]


def test_slack_defect_and_feedback_create_projectctl_items_without_triage(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    created: list[tuple] = []

    def fake_defect(root, *, title, description=None, severity="medium"):
        created.append(("defect", title, description, severity, str(root)))
        return SimpleNamespace(stdout="Created BUG-SLACK-1\n", returncode=0)

    def fake_story(root, entity, *, title, description=None):
        created.append((entity, title, description, str(root)))
        return SimpleNamespace(stdout="Created STORY-SLACK-1\n", returncode=0)

    monkeypatch.setattr("projectos.slack_commands.create_defect", fake_defect)
    monkeypatch.setattr("projectos.slack_commands.create_projectctl_entity", fake_story)

    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    client.post(
        "/v1/projects/PRJ-A/integrations/slack/bind",
        json={"channel_id": "CBUG", "team_id": "T1"},
    )
    forbidden = client.post(
        "/v1/integrations/slack/command",
        json={
            "command": "defect",
            "channel_id": "CBUG",
            "team_id": "T1",
            "title": "Login fails",
            "severity": "critical",
            "priority": "p0",
        },
    )
    assert forbidden.status_code == 422, forbidden.text

    defect = client.post(
        "/v1/integrations/slack/command",
        json={
            "command": "defect",
            "channel_id": "CBUG",
            "team_id": "T1",
            "message_ts": "171234.1",
            "title": "Login fails",
            "description": "Button does nothing",
            "source": "qa",
        },
    )
    assert defect.status_code == 200, defect.text
    payload = defect.json()
    assert payload["item_kind"] == "defect"
    assert payload["item_human_id"] == "BUG-SLACK-1"
    assert payload["item_human_id"] in payload["text"]
    assert "severity" not in payload["text"].lower() or "not set from slack" in payload["text"].lower()
    assert created[0][0] == "defect"
    assert created[0][3] == "medium"
    assert "alpha" not in (created[0][2] or "")

    again = client.post(
        "/v1/integrations/slack/command",
        json={
            "command": "defect",
            "channel_id": "CBUG",
            "team_id": "T1",
            "message_ts": "171234.1",
            "title": "Login fails",
            "source": "qa",
        },
    )
    assert again.json()["idempotent"] is True
    assert again.json()["item_human_id"] == "BUG-SLACK-1"
    assert len(created) == 1

    feedback = client.post(
        "/v1/integrations/slack/command",
        json={
            "command": "feedback",
            "channel_id": "CBUG",
            "team_id": "T1",
            "message_ts": "171234.2",
            "title": "Please add a darker theme",
            "source": "customer",
        },
    )
    assert feedback.status_code == 200, feedback.text
    assert feedback.json()["item_kind"] == "feedback"
    assert feedback.json()["item_human_id"] == "STORY-SLACK-1"
    assert created[1][0] == "story"


def test_slack_slash_command_returns_slack_message_for_bound_channel(tmp_path: Path) -> None:
    from urllib.parse import urlencode

    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    bound = client.post(
        "/v1/projects/PRJ-A/integrations/slack/bind",
        json={"channel_id": "CSLASH", "team_id": "T1"},
    )
    assert bound.status_code == 200, bound.text
    status = client.post(
        "/v1/integrations/slack/slash",
        content=urlencode(
            {
                "command": "/projectos",
                "text": "status",
                "channel_id": "CSLASH",
                "team_id": "T1",
                "user_name": "tyler",
            }
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert status.status_code == 200, status.text
    body = status.json()
    assert "PRJ-A" in body["text"]
    assert body["response_type"] in {"in_channel", "ephemeral"}

    unknown = client.post(
        "/v1/integrations/slack/slash",
        content=urlencode({"command": "/projectos", "text": "approve", "channel_id": "CSLASH"}),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert unknown.status_code == 200, unknown.text
    assert "Unknown" in unknown.json()["text"] or "unknown" in unknown.json()["text"].lower()


def test_http_slash_explicit_project_works(tmp_path: Path) -> None:
    from urllib.parse import urlencode

    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    from projectos.db import connection
    from projectos.store import add_slack_interface_channel

    with connection(tmp_path / "projectos.db") as conn:
        add_slack_interface_channel(conn, channel_id="CSLASH", team_id="T1", is_default=True)
    status = client.post(
        "/v1/integrations/slack/slash",
        content=urlencode(
            {
                "command": "/projectos",
                "text": "PRJ-B status",
                "channel_id": "CSLASH",
                "team_id": "T1",
                "user_name": "tyler",
            }
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert status.status_code == 200, status.text
    body = status.json()
    assert "PRJ-B" in body["text"]


def test_slack_settings_api_read_write_and_hides_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    read = client.get("/v1/settings/integrations/slack")
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["mode"] == "socket"
    assert body["app_token"] == "xapp-...configured"
    assert "xapp-secret-app" not in read.text
    updated = client.put(
        "/v1/settings/integrations/slack",
        json={
            "add_interface_channels": [{"channel_id": "CSETTINGS", "team_id": "T1", "is_default": True}],
        },
    )
    assert updated.status_code == 200, updated.text
    channels = updated.json()["interface_channels"]
    assert any(item["channel_id"] == "CSETTINGS" for item in channels)
    assert updated.json()["default_channel_id"] == "CSETTINGS"


def test_slack_tokens_api_saves_without_echoing_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PROJECTOS_SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("PROJECTOS_SLACK_BOT_TOKEN", raising=False)
    secrets_path = tmp_path / "slack_secrets.enc"
    monkeypatch.setattr("projectos.secret_store._secrets_file", lambda: secrets_path)
    from projectos.slack_tokens import reload_slack_tokens

    reload_slack_tokens()
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    response = client.post(
        "/v1/settings/integrations/slack/tokens",
        json={
            "app_token": "xapp-api-secret",
            "bot_token": "xoxb-api-secret",
            "signing_secret": "signing-secret-value",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["app_token"] == "xapp-...configured"
    assert body["bot_token"] == "xoxb-...configured"
    assert body["signing_secret_present"] is True
    assert body["storage"] == "encrypted_local_store"
    assert "xapp-api-secret" not in response.text
    assert "xoxb-api-secret" not in response.text
    assert "signing-secret-value" not in response.text
    from projectos.secret_store import read_slack_secrets

    stored = read_slack_secrets(secrets_path=secrets_path)
    assert stored["app_token"] == "xapp-api-secret"
    assert stored["bot_token"] == "xoxb-api-secret"
    reload_slack_tokens()


def test_slack_status_endpoint_does_not_expose_tokens(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_APP_TOKEN", "xapp-secret-app")
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-secret-bot")
    client = _client(tmp_path)
    response = client.get("/v1/integrations/slack/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "socket"
    assert body["app_token"] == "xapp-...configured"
    assert body["bot_token"] == "xoxb-...configured"
    assert "xapp-secret-app" not in response.text
    assert "xoxb-secret-bot" not in response.text
    assert "setup_steps" in body

