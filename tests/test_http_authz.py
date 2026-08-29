"""Local authz denies disallowed operator, admin, approval, and Slack actions."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.http import create_app
from projectos.migrate import initialize_database

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

_ACTORS = {
    "reader": "reader",
    "operator": "operator",
    "admin": "admin",
    "approver": "approver",
    "slack-bot": "slack",
}


def _client(tmp_path: Path) -> TestClient:
    alpha = init_git_repo(tmp_path / "alpha")
    write_identity(alpha, project_human_id="PRJ-A", project_name="Alpha")
    write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-A",
                "repository_root": str(alpha.resolve()),
                "enabled": True,
            }
        ],
    )
    return TestClient(
        create_app(
            registry_path=tmp_path / "projects.json",
            db_path=tmp_path / "projectos.db",
            projectctl_runner=lambda root: fake_status("PRJ-A"),
            skip_identity_validation=True,
            auth_required=True,
            actors=_ACTORS,
        )
    )


def _h(actor: str) -> dict[str, str]:
    return {"X-ProjectOS-Actor": actor}


def test_negative_authz_by_role(tmp_path: Path) -> None:
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")

    health = client.get("/health")
    assert health.status_code == 200, health.text

    missing = client.get("/v1/projects")
    assert missing.status_code == 403, missing.text
    assert missing.json()["error"]["code"] == "forbidden"

    unknown = client.get("/v1/projects", headers=_h("ghost"))
    assert unknown.status_code == 403, unknown.text

    listed = client.get("/v1/projects", headers=_h("reader"))
    assert listed.status_code == 200, listed.text

    quality = client.get("/v1/projects/PRJ-A/quality", headers=_h("reader"))
    assert quality.status_code == 200, quality.text

    bind_reader = client.post(
        "/v1/projects/PRJ-A/integrations/slack/bind",
        headers=_h("reader"),
        json={"channel_id": "C1", "team_id": "T1"},
    )
    assert bind_reader.status_code == 403, bind_reader.text

    bind_op = client.post(
        "/v1/projects/PRJ-A/integrations/slack/bind",
        headers=_h("operator"),
        json={"channel_id": "C1", "team_id": "T1"},
    )
    assert bind_op.status_code == 200, bind_op.text

    notify_reader = client.post(
        "/v1/projects/PRJ-A/integrations/slack/notify",
        headers=_h("reader"),
    )
    assert notify_reader.status_code == 403, notify_reader.text

    notify_op = client.post(
        "/v1/projects/PRJ-A/integrations/slack/notify",
        headers=_h("operator"),
    )
    assert notify_op.status_code == 200, notify_op.text

    unbind_op = client.post(
        "/v1/projects/PRJ-A/integrations/slack/unbind",
        headers=_h("operator"),
        json={"channel_id": "C1", "team_id": "T1"},
    )
    assert unbind_op.status_code == 403, unbind_op.text

    command_reader = client.post(
        "/v1/integrations/slack/command",
        headers=_h("reader"),
        json={"command": "status", "channel_id": "C1", "team_id": "T1"},
    )
    assert command_reader.status_code == 403, command_reader.text

    command_slack = client.post(
        "/v1/integrations/slack/command",
        headers=_h("slack-bot"),
        json={"command": "status", "channel_id": "C1", "team_id": "T1"},
    )
    assert command_slack.status_code == 200, command_slack.text

    inbound_reader = client.post(
        "/v1/integrations/slack/inbound",
        headers=_h("reader"),
        json={"channel_id": "C1", "team_id": "T1", "message_ts": "1.1"},
    )
    assert inbound_reader.status_code == 403, inbound_reader.text

    inbound_slack = client.post(
        "/v1/integrations/slack/inbound",
        headers=_h("slack-bot"),
        json={"channel_id": "C1", "team_id": "T1", "message_ts": "1.1"},
    )
    assert inbound_slack.status_code == 200, inbound_slack.text

    opened = client.post(
        "/v1/projects/PRJ-A/decisions",
        headers=_h("operator"),
        json={
            "action": "governance_change",
            "reason": "Needs sponsor grant",
            "impact": "Changes governed process",
            "requested_by": "operator",
        },
    )
    assert opened.status_code == 200, opened.text
    decision_id = opened.json()["decision_human_id"]

    approve_op = client.post(
        f"/v1/projects/PRJ-A/decisions/{decision_id}/approve",
        headers=_h("operator"),
        json={"confirmed": True, "actor": "operator", "reason": "looks good"},
    )
    assert approve_op.status_code == 403, approve_op.text

    approve_admin = client.post(
        f"/v1/projects/PRJ-A/decisions/{decision_id}/approve",
        headers=_h("admin"),
        json={"confirmed": True, "actor": "admin", "reason": "looks good"},
    )
    assert approve_admin.status_code == 403, approve_admin.text

    approve_ok = client.post(
        f"/v1/projects/PRJ-A/decisions/{decision_id}/approve",
        headers=_h("approver"),
        json={"confirmed": True, "actor": "approver", "reason": "granted"},
    )
    assert approve_ok.status_code == 200, approve_ok.text

    retire_op = client.post(
        "/v1/projects/PRJ-A/learning/memories/MEM-1/retire",
        headers=_h("operator"),
        json={"confirmed": True, "reason": "stale", "actor": "operator"},
    )
    assert retire_op.status_code == 403, retire_op.text

    recover_op = client.post(
        "/v1/projects/PRJ-A/recovery/execute",
        headers=_h("operator"),
        json={},
    )
    assert recover_op.status_code == 403, recover_op.text

    disable_op = client.post(
        "/v1/projects/PRJ-A/disable",
        headers=_h("operator"),
    )
    assert disable_op.status_code == 403, disable_op.text

    unbind_admin = client.post(
        "/v1/projects/PRJ-A/integrations/slack/unbind",
        headers=_h("admin"),
        json={"channel_id": "C1", "team_id": "T1"},
    )
    assert unbind_admin.status_code == 200, unbind_admin.text
