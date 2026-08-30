"""Project repository isolation enforcement tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from projectctl import store
from projectctl.cli import main
from projectctl.db import connect
from projectctl.isolation import ProjectIsolationError, validate_project_isolation
from projectctl.migrate import initialize_database
from projectctl.repository import RepositoryIdentityError, load_repository_identity


def _write_identity(
    repo_root: Path,
    *,
    project_human_id: str = "PRJ-003",
    project_name: str = "Personal Task Manager Pilot",
    corrupt: bool = False,
    omit_keys: tuple[str, ...] = (),
    overrides: dict | None = None,
) -> Path:
    project_dir = repo_root / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / "repository.json"
    if corrupt:
        path.write_text("{not-json", encoding="utf-8")
        return path
    data = {
        "schema_version": 1,
        "repository_type": "delivery-project",
        "project_human_id": project_human_id,
        "project_name": project_name,
        "isolation_model": "one-project-per-repository",
        "orchestration_scope": "project",
        "cross_project_access": False,
    }
    if overrides:
        data.update(overrides)
    for key in omit_keys:
        data.pop(key, None)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _seed_prj003_with_history(db: Path) -> None:
    """PRJ-001/002 inactive smoke + PRJ-003 active (mirrors production posture)."""
    store.create_project("Smoke Test", db_path=db, make_active=False)
    store.create_project(
        "Project Control Smoke Test", db_path=db, make_active=False
    )
    store.create_project(
        "Personal Task Manager Pilot", db_path=db, make_active=True
    )


def test_valid_prj003_identity_passes(tmp_path: Path) -> None:
    db = tmp_path / "ok.db"
    initialize_database(db_path=db)
    repo = tmp_path / "repo"
    _write_identity(repo, project_human_id="PRJ-003")
    _seed_prj003_with_history(db)

    result = validate_project_isolation(db_path=db, repo_root=repo)
    assert result.active_project_human_id == "PRJ-003"
    assert result.active_count == 1

    assert main(["--db", str(db), "--repo-root", str(repo), "status"]) == 0
    assert (
        main(
            [
                "--db",
                str(db),
                "--repo-root",
                str(repo),
                "requirement",
                "list",
            ]
        )
        == 0
    )


def test_identity_mismatch_fails(tmp_path: Path) -> None:
    db = tmp_path / "mismatch.db"
    initialize_database(db_path=db)
    repo = tmp_path / "repo"
    _write_identity(repo, project_human_id="PRJ-003")
    store.create_project("Wrong Active", db_path=db, make_active=True)  # PRJ-001

    with pytest.raises(ProjectIsolationError, match="does not match"):
        validate_project_isolation(db_path=db, repo_root=repo)

    assert main(["--db", str(db), "--repo-root", str(repo), "status"]) == 1


def test_zero_active_projects_fails(tmp_path: Path) -> None:
    db = tmp_path / "zero.db"
    initialize_database(db_path=db)
    repo = tmp_path / "repo"
    _write_identity(repo, project_human_id="PRJ-003")
    store.create_project("Inactive Only", db_path=db, make_active=False)

    with pytest.raises(ProjectIsolationError, match="no active project"):
        validate_project_isolation(db_path=db, repo_root=repo)

    assert main(["--db", str(db), "--repo-root", str(repo), "status"]) == 1


def test_more_than_one_active_fails(tmp_path: Path) -> None:
    db = tmp_path / "multi.db"
    initialize_database(db_path=db)
    repo = tmp_path / "repo"
    _write_identity(repo, project_human_id="PRJ-001")
    store.create_project("A", db_path=db, make_active=True)
    # Force a second active without going through create_project deactivation.
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO projects (human_id, name, status, is_active) "
            "VALUES ('PRJ-002', 'B', 'active', 1)"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ProjectIsolationError, match="multiple active"):
        validate_project_isolation(db_path=db, repo_root=repo)

    assert main(["--db", str(db), "--repo-root", str(repo), "status"]) == 1


def test_missing_repository_json_fails(tmp_path: Path) -> None:
    db = tmp_path / "missing.db"
    initialize_database(db_path=db)
    store.create_project("X", db_path=db)
    repo = tmp_path / "empty-repo"
    repo.mkdir()

    with pytest.raises(RepositoryIdentityError, match="not found|missing"):
        load_repository_identity(repo)

    assert main(["--db", str(db), "--repo-root", str(repo), "status"]) == 1


def test_malformed_repository_json_fails(tmp_path: Path) -> None:
    db = tmp_path / "badjson.db"
    initialize_database(db_path=db)
    store.create_project("X", db_path=db)
    repo = tmp_path / "repo"
    _write_identity(repo, corrupt=True)

    with pytest.raises(RepositoryIdentityError, match="malformed"):
        load_repository_identity(repo)

    assert main(["--db", str(db), "--repo-root", str(repo), "status"]) == 1


def test_inactive_historical_projects_do_not_cause_failure(tmp_path: Path) -> None:
    db = tmp_path / "hist.db"
    initialize_database(db_path=db)
    repo = tmp_path / "repo"
    _write_identity(repo, project_human_id="PRJ-003")
    _seed_prj003_with_history(db)

    result = validate_project_isolation(db_path=db, repo_root=repo)
    assert result.active_project_human_id == "PRJ-003"
    rows = store.list_projects(db_path=db)
    assert [r["human_id"] for r in rows] == ["PRJ-001", "PRJ-002", "PRJ-003"]
    assert [r["is_active"] for r in rows] == [0, 0, 1]


def test_reject_create_second_active_unrelated_project(tmp_path: Path) -> None:
    db = tmp_path / "second.db"
    initialize_database(db_path=db)
    repo = tmp_path / "repo"
    _write_identity(repo, project_human_id="PRJ-003")
    _seed_prj003_with_history(db)

    with pytest.raises(store.StoreError, match="cannot create another active"):
        store.create_project(
            "Unrelated",
            db_path=db,
            make_active=True,
            enforce_isolation=True,
            repo_root=repo,
        )

    # CLI create also rejected
    assert (
        main(
            [
                "--db",
                str(db),
                "--repo-root",
                str(repo),
                "project",
                "create",
                "--name",
                "Unrelated Two",
            ]
        )
        == 1
    )

    # Inactive create still allowed; does not delete history
    assert (
        main(
            [
                "--db",
                str(db),
                "--repo-root",
                str(repo),
                "project",
                "create",
                "--name",
                "Extra Smoke",
                "--inactive",
            ]
        )
        == 0
    )
    rows = store.list_projects(db_path=db)
    assert any(r["human_id"] == "PRJ-001" and r["is_active"] == 0 for r in rows)
    assert any(r["human_id"] == "PRJ-002" and r["is_active"] == 0 for r in rows)
    assert any(r["human_id"] == "PRJ-003" and r["is_active"] == 1 for r in rows)


def test_reject_activate_unrelated_project(tmp_path: Path) -> None:
    db = tmp_path / "act.db"
    initialize_database(db_path=db)
    repo = tmp_path / "repo"
    _write_identity(repo, project_human_id="PRJ-003")
    _seed_prj003_with_history(db)

    with pytest.raises(store.StoreError, match="cannot activate PRJ-001"):
        store.activate_project(
            "PRJ-001",
            db_path=db,
            enforce_isolation=True,
            repo_root=repo,
        )

    assert (
        main(
            [
                "--db",
                str(db),
                "--repo-root",
                str(repo),
                "project",
                "activate",
                "PRJ-001",
            ]
        )
        == 1
    )


def test_unsupported_isolation_model_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_identity(
        repo,
        overrides={"isolation_model": "shared-database"},
    )
    with pytest.raises(RepositoryIdentityError, match="isolation_model"):
        load_repository_identity(repo)
