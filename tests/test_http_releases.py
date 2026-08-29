"""Release center is ID-keyed; downloads never take a filesystem path."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.errors import OrchestrationError
from projectos.http import create_app
from projectos.migrate import initialize_database
from projectos.store import (
    create_job,
    require_safe_id,
    set_job_outcome,
    set_job_source_provenance,
    upsert_release_artifact,
)

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
    app = create_app(
        registry_path=tmp_path / "projects.json",
        db_path=tmp_path / "projectos.db",
        projectctl_runner=lambda root: fake_status("PRJ-A"),
    )
    return TestClient(app)


def test_require_safe_id_rejects_path_traversal() -> None:
    with pytest.raises(OrchestrationError):
        require_safe_id("../secret", label="artifact")
    with pytest.raises(OrchestrationError):
        require_safe_id("C:\\\\Windows\\\\secret.txt", label="artifact")
    with pytest.raises(OrchestrationError):
        require_safe_id("a/b", label="artifact")


def test_release_center_lists_detail_and_allowlisted_download(tmp_path: Path) -> None:
    client = _client(tmp_path)
    initialize_database(tmp_path / "projectos.db")
    repo = tmp_path / "alpha"
    notes = "# REL-1 notes\nShip only after QA gate ready.\n"
    rollback = "Rollback: revert to previous integrated SHA.\n"
    with connection(tmp_path / "projectos.db") as conn:
        release = create_job(
            conn,
            human_id="REL-1",
            project_human_id="PRJ-A",
            repository_root=repo,
            agent_role="RELEASE",
            queue="RELEASE",
            status="SUCCEEDED",
            iteration_human_id="ITER-1",
        )
        set_job_outcome(conn, release.id, outcome="GATE_READY")
        set_job_source_provenance(
            conn,
            release.id,
            source_delivery_job_id=None,
            source_candidate_sha="intsha001122",
        )
        conn.execute(
            "UPDATE orchestration_jobs SET candidate_git_sha = ? WHERE id = ?",
            ("relsha998877", release.id),
        )
        upsert_release_artifact(
            conn,
            project_human_id="PRJ-A",
            release_human_id="REL-1",
            artifact_human_id="release-notes",
            filename="release-notes.md",
            content=notes.encode("utf-8"),
            kind="notes",
            media_type="text/markdown",
        )
        upsert_release_artifact(
            conn,
            project_human_id="PRJ-A",
            release_human_id="REL-1",
            artifact_human_id="rollback-notes",
            filename="rollback.md",
            content=rollback.encode("utf-8"),
            kind="rollback",
            media_type="text/markdown",
        )
        with pytest.raises(OrchestrationError):
            upsert_release_artifact(
                conn,
                project_human_id="PRJ-A",
                release_human_id="REL-1",
                artifact_human_id="evil",
                filename="../passwd",
                content=b"nope",
                kind="notes",
            )

    listed = client.get("/v1/projects/PRJ-A/releases")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["releases"][0]["release_human_id"] == "REL-1"
    assert body["releases"][0]["gate"] == "ready"
    assert body["releases"][0]["artifact_count"] == 2

    detail = client.get("/v1/projects/PRJ-A/releases/REL-1")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["gate"] == "ready"
    assert payload["integrated_sha"] == "intsha001122"
    assert payload["released_sha"] == "relsha998877"
    assert "Ship only after QA" in payload["release_notes"]
    assert "Rollback" in payload["rollback_notes"]
    names = {item["filename"] for item in payload["manifest"]["files"]}
    assert names == {"release-notes.md", "rollback.md"}
    assert payload["artifacts"][0]["download_ref"].startswith("/v1/projects/PRJ-A/releases/")
    dumped = detail.text
    assert "repository_root" not in dumped
    assert "C:\\" not in dumped
    assert "../" not in dumped

    download = client.get("/v1/projects/PRJ-A/releases/REL-1/artifacts/release-notes")
    assert download.status_code == 200
    assert download.content == notes.encode("utf-8")
    assert "release-notes.md" in download.headers.get("content-disposition", "")
    assert "nosniff" in download.headers.get("x-content-type-options", "")

    missing = client.get("/v1/projects/PRJ-A/releases/REL-1/artifacts/not-real")
    assert missing.status_code == 404
    traversal = client.get("/v1/projects/PRJ-A/releases/REL-1/artifacts/..%2Fsecret")
    assert traversal.status_code == 404
    slash = client.get("/v1/projects/PRJ-A/releases/REL-1/artifacts/a/b")
    assert slash.status_code == 404
    no_path = client.get(
        "/v1/projects/PRJ-A/releases/REL-1/artifacts/release-notes",
        params={"path": str(tmp_path / "secret.bin")},
    )
    assert no_path.status_code == 200
    assert no_path.content == notes.encode("utf-8")
