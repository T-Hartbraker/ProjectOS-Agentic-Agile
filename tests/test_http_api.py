"""Thin HTTP control-plane tests. Routes must not browse the filesystem."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from projectos.http import create_app


def _delivery_repo(tmp_path: Path, name: str, human_id: str) -> Path:
    repo = init_git_repo(tmp_path / name)
    write_identity(repo, project_human_id=human_id, project_name="Example")
    return repo


def _client(tmp_path: Path, *, runner=None) -> TestClient:
    registry = tmp_path / "projects.json"
    app = create_app(
        registry_path=registry,
        db_path=tmp_path / "projectos.db",
        projectctl_runner=runner or (lambda root: fake_status("PRJ-A")),
    )
    return TestClient(app)


def test_health_and_correlation_id(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get("/health", headers={"X-Correlation-ID": "cid-123"})
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "projectos"
    assert body["status"] in {"ok", "degraded"}
    api = next(item for item in body["components"] if item["name"] == "api")
    assert api["status"] == "ok"
    assert body["notice"]
    assert response.headers["X-Correlation-ID"] == "cid-123"
    v1 = client.get("/v1/health")
    assert v1.status_code == 200
    assert v1.headers["X-Correlation-ID"]
    assert v1.json()["service"] == "projectos"
    assert v1.json()["status"] in {"ok", "degraded"}


def test_built_dashboard_is_served_from_api(tmp_path: Path) -> None:
    from projectos.paths import dashboard_is_built

    if not dashboard_is_built():
        pytest.skip("web/dist is not built")
    client = _client(tmp_path)
    response = client.get("/")
    assert response.status_code == 200, response.text
    assert "<!doctype html>" in response.text.lower() or "<html" in response.text.lower()
    projects = client.get("/v1/projects")
    assert projects.status_code == 200


def test_cors_allows_dashboard_origin(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get(
        "/v1/health",
        headers={"Origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_list_empty_and_register_detail_disable(tmp_path: Path) -> None:
    repo = _delivery_repo(tmp_path, "alpha", "PRJ-A")
    client = _client(tmp_path, runner=lambda root: fake_status("PRJ-A"))
    empty = client.get("/v1/projects")
    assert empty.status_code == 200
    assert empty.json() == {"projects": []}

    created = client.post(
        "/v1/projects",
        json={"repository_path": str(repo.resolve())},
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["action"] == "registered"
    assert payload["project_human_id"] == "PRJ-A"
    assert payload["enabled"] is True
    assert Path(payload["repository_root"]).resolve() == repo.resolve()

    listed = client.get("/v1/projects")
    assert listed.status_code == 200
    assert len(listed.json()["projects"]) == 1

    detail = client.get("/v1/projects/PRJ-A")
    assert detail.status_code == 200
    assert detail.json()["project_human_id"] == "PRJ-A"

    missing = client.get("/v1/projects/PRJ-MISSING")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert missing.json()["error"]["correlation_id"]

    disabled = client.post("/v1/projects/PRJ-A/disable")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["action"] == "disabled"


def test_register_rejects_relative_path_and_extra_fields(tmp_path: Path) -> None:
    client = _client(tmp_path)
    relative = client.post("/v1/projects", json={"repository_path": "relative/repo"})
    assert relative.status_code == 422
    assert relative.json()["error"]["code"] == "validation_error"

    extra = client.post(
        "/v1/projects",
        json={"repository_path": str(tmp_path.resolve()), "read_file": "/etc/passwd"},
    )
    assert extra.status_code == 422


def test_register_conflict_is_409(tmp_path: Path) -> None:
    repo = _delivery_repo(tmp_path, "alpha", "PRJ-A")
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
    client = _client(tmp_path, runner=lambda root: fake_status("PRJ-A"))
    response = client.post(
        "/v1/projects",
        json={"repository_path": str(repo.resolve())},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_no_filesystem_browse_endpoint(tmp_path: Path) -> None:
    client = _client(tmp_path)
    for path in ("/files", "/v1/files", "/v1/fs", "/v1/read"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
