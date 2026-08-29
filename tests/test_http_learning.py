"""Learning dashboard exposes auto-learned AGENT_MEMORY, not an approval queue."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.errors import CrossProjectWriteError
from projectos.http import create_app
from projectos.learning import ingest_agent_memory, record_injections
from projectos.migrate import initialize_database
from projectos.store import create_job

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _client(tmp_path: Path) -> TestClient:
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-A", project_name="Example")
    write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-A",
                "repository_root": str(repo.resolve()),
                "enabled": True,
            }
        ],
    )
    return TestClient(
        create_app(
            registry_path=tmp_path / "projects.json",
            db_path=tmp_path / "projectos.db",
            projectctl_runner=lambda root: fake_status("PRJ-A"),
        )
    )


def test_safe_agent_memory_is_auto_learned_without_approval(tmp_path: Path) -> None:
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    repo = tmp_path / "alpha"
    with connection(tmp_path / "projectos.db") as conn:
        delivery = create_job(
            conn,
            human_id="DEL-1",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
        )
        learned = ingest_agent_memory(
            conn,
            project_human_id="PRJ-A",
            agent_role="DELIVERY",
            title="Prefer bounded retries for transient git lock errors",
            evidence_ref=r"C:\secret\runs\notes.md",
            source_job_human_id=delivery.human_id,
            confidence=0.6,
        )
        assert learned["status"] == "ACTIVE"
        assert learned["promotion_mode"] == "AUTO_LEARNED"
        assert learned["evidence_ref"] == "notes.md"
        reinforced = ingest_agent_memory(
            conn,
            project_human_id="PRJ-A",
            agent_role="DELIVERY",
            title="Prefer bounded retries for transient git lock errors",
            source_job_human_id=delivery.human_id,
        )
        record_injections(
            conn,
            project_human_id="PRJ-A",
            job_human_id=delivery.human_id,
            agent_run_id=None,
            memories=[reinforced],
        )
        rejected = ingest_agent_memory(
            conn,
            project_human_id="PRJ-A",
            agent_role="DELIVERY",
            title="C:\\secret\\payload",
            memory_kind="AGENT_MEMORY",
        )
        assert rejected["status"] == "REJECTED"
        assert rejected["rejection_code"] == "unsafe_content"
        other = ingest_agent_memory(
            conn,
            project_human_id="PRJ-A",
            agent_role="DELIVERY",
            title="Sponsor policy change",
            memory_kind="POLICY",
        )
        assert other["status"] == "REJECTED"
        assert other["rejection_code"] == "not_agent_memory"

    body = client.get("/v1/projects/PRJ-A/learning")
    assert body.status_code == 200, body.text
    payload = body.json()
    assert "auto-learned" in payload["notice"].lower() or "approval" in payload["notice"].lower()
    assert payload["active_memories"][0]["status"] == "ACTIVE"
    assert payload["active_memories"][0]["promotion_mode"] == "AUTO_LEARNED"
    assert payload["active_memories"][0]["evidence_ref"] == "notes.md"
    assert payload["active_memories"][0]["occurrence_count"] == 2
    assert payload["active_memories"][0]["agent_role"] == "DELIVERY"
    events = {item["event_type"] for item in payload["events"]}
    assert "promoted" in events
    assert "reinforced" in events
    assert "rejected" in events
    assert payload["injected_in_recent_runs"][0]["memory_human_id"] == payload["active_memories"][0]["memory_human_id"]
    assert payload["injected_in_recent_runs"][0]["job_human_id"] == "DEL-1"
    codes = {item["rejection_code"] for item in payload["rejected_memories"]}
    assert "unsafe_content" in codes
    assert "C:\\secret" not in body.text

    denied = client.post("/v1/projects/PRJ-A/learning/approve", json={})
    assert denied.status_code in {404, 405}


def test_learning_ingest_rejects_foreign_job(tmp_path: Path) -> None:
    _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    repo = tmp_path / "alpha"
    with connection(tmp_path / "projectos.db") as conn:
        create_job(
            conn,
            human_id="DEL-A",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
        )
        with pytest.raises(CrossProjectWriteError):
            ingest_agent_memory(
                conn,
                project_human_id="PRJ-B",
                agent_role="DELIVERY",
                title="Do not leak across projects",
                source_job_human_id="DEL-A",
            )



def test_retire_and_supersede_require_confirmation_and_preserve_history(tmp_path: Path) -> None:
    from projectos.cli import main
    from projectos.errors import OrchestrationError
    from projectos.learning import list_active_memories_for_prompt, retire_memory, supersede_memory

    client = _client(tmp_path)
    db = tmp_path / "projectos.db"
    config = tmp_path / "projects.json"
    initialize_database(db)
    repo = tmp_path / "alpha"
    with connection(db) as conn:
        create_job(
            conn,
            human_id="DEL-1",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="SUCCEEDED",
        )
        first = ingest_agent_memory(
            conn,
            project_human_id="PRJ-A",
            agent_role="DELIVERY",
            title="Prefer bounded retries for transient git lock errors",
            evidence_ref="notes.md",
            source_job_human_id="DEL-1",
        )
        second = ingest_agent_memory(
            conn,
            project_human_id="PRJ-A",
            agent_role="DELIVERY",
            title="Keep release notes basename-only",
            evidence_ref="gate.md",
            source_job_human_id="DEL-1",
        )
        mem_id = first["memory_human_id"]
        other_id = second["memory_human_id"]
        with pytest.raises(OrchestrationError, match="confirmation is required"):
            retire_memory(
                conn,
                project_human_id="PRJ-A",
                memory_human_id=mem_id,
                confirmed=False,
                reason="stale guidance",
                actor="operator",
            )
        with pytest.raises(OrchestrationError, match="reason is required"):
            retire_memory(
                conn,
                project_human_id="PRJ-A",
                memory_human_id=mem_id,
                confirmed=True,
                reason="  ",
                actor="operator",
            )

    missing = client.post(
        f"/v1/projects/PRJ-A/learning/memories/{mem_id}/retire",
        json={"confirmed": False, "reason": "stale guidance", "actor": "operator"},
    )
    assert missing.status_code == 409, missing.text
    markdown = client.post(
        f"/v1/projects/PRJ-A/learning/memories/{mem_id}/retire",
        json={
            "confirmed": True,
            "reason": "stale guidance",
            "actor": "operator",
            "markdown_body": "# edit me",
        },
    )
    assert markdown.status_code == 422, markdown.text

    retired = client.post(
        f"/v1/projects/PRJ-A/learning/memories/{mem_id}/retire",
        json={"confirmed": True, "reason": "stale guidance", "actor": "operator"},
    )
    assert retired.status_code == 200, retired.text
    retired_body = retired.json()
    assert retired_body["action"] == "retire"
    assert retired_body["actor"] == "operator"
    assert retired_body["memory"]["status"] == "RETIRED"
    assert retired_body["memory"]["memory_human_id"] == mem_id

    view = client.get("/v1/projects/PRJ-A/learning")
    assert view.status_code == 200, view.text
    payload = view.json()
    active_ids = {item["memory_human_id"] for item in payload["active_memories"]}
    retired_ids = {item["memory_human_id"] for item in payload["retired_memories"]}
    assert mem_id not in active_ids
    assert mem_id in retired_ids
    assert other_id in active_ids
    events = payload["events"]
    assert any(
        item["event_type"] == "retired" and item["actor"] == "operator" and item["memory_human_id"] == mem_id
        for item in events
    )
    assert any(item["event_type"] == "promoted" and item["memory_human_id"] == mem_id for item in events)

    with connection(db) as conn:
        injected = list_active_memories_for_prompt(conn, "PRJ-A", "DELIVERY")
        assert all(item["memory_human_id"] != mem_id for item in injected)
        assert any(item["memory_human_id"] == other_id for item in injected)

    superseded = client.post(
        f"/v1/projects/PRJ-A/learning/memories/{other_id}/supersede",
        json={
            "confirmed": True,
            "reason": "narrower retry rule",
            "actor": "operator",
            "successor_title": "Retry git locks at most three times",
        },
    )
    assert superseded.status_code == 200, superseded.text
    superseded_body = superseded.json()
    assert superseded_body["action"] == "supersede"
    assert superseded_body["memory"]["status"] == "SUPERSEDED"
    successor_id = superseded_body["successor"]["memory_human_id"]
    assert successor_id != other_id
    assert superseded_body["successor"]["status"] == "ACTIVE"
    assert superseded_body["memory"]["superseded_by_memory_human_id"] == successor_id

    view = client.get("/v1/projects/PRJ-A/learning")
    payload = view.json()
    assert other_id not in {item["memory_human_id"] for item in payload["active_memories"]}
    assert successor_id in {item["memory_human_id"] for item in payload["active_memories"]}
    assert other_id in {item["memory_human_id"] for item in payload["superseded_memories"]}
    assert any(
        item["event_type"] == "superseded" and item["actor"] == "operator"
        for item in payload["events"]
    )

    denied = client.post("/v1/projects/PRJ-A/learning/approve", json={})
    assert denied.status_code in {404, 405}

    assert (
        main(
            [
                "--config",
                str(config),
                "learning",
                "retire",
                "--project",
                "PRJ-A",
                "--memory",
                mem_id,
                "--actor",
                "operator",
                "--reason",
                "already retired",
                "--db",
                str(db),
            ]
        )
        == 1
    )
    assert (
        main(
            [
                "--config",
                str(config),
                "learning",
                "retire",
                "--project",
                "PRJ-A",
                "--memory",
                mem_id,
                "--actor",
                "operator",
                "--reason",
                "already retired",
                "--confirm",
                "--db",
                str(db),
            ]
        )
        == 1
    )
