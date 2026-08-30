"""Delivery-template initialization and prepare tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from projectctl import store
from projectctl.cli import main
from projectctl.db import connect
from projectctl.isolation import ProjectIsolationError, validate_project_isolation
from projectctl.migrate import initialize_database
from projectctl.repository import (
    load_repository_identity,
    template_manifest,
    write_repository_identity,
)
from projectctl.template_ops import init_repository, prepare_template


def _mini_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create an isolated mini delivery repo with its own project-control DB."""
    root = tmp_path / "delivery-repo"
    (root / "project").mkdir(parents=True)
    (root / "project-control" / "migrations").mkdir(parents=True)
    # Point package migrations by using --db under this repo; initialize via API.
    db = root / "project-control" / "project.db"
    initialize_database(db_path=db)
    return root, db


def _write_template(root: Path) -> None:
    write_repository_identity(root, template_manifest())


def _write_bound(root: Path, human_id: str = "PRJ-003", name: str = "Bound") -> None:
    write_repository_identity(
        root,
        {
            "schema_version": 1,
            "repository_type": "delivery-project",
            "project_human_id": human_id,
            "project_name": name,
            "isolation_model": "one-project-per-repository",
            "orchestration_scope": "project",
            "cross_project_access": False,
        },
    )


def test_delivery_template_identity_parses(tmp_path: Path) -> None:
    root, _db = _mini_repo(tmp_path)
    _write_template(root)
    ident = load_repository_identity(root)
    assert ident.is_template
    assert ident.project_human_id is None
    assert ident.project_name is None
    assert ident.isolation_model == "one-project-per-repository"


def test_status_fails_while_unbound(tmp_path: Path) -> None:
    root, db = _mini_repo(tmp_path)
    _write_template(root)
    with pytest.raises(ProjectIsolationError, match="delivery-template"):
        validate_project_isolation(db_path=db, repo_root=root)
    assert main(["--db", str(db), "--repo-root", str(root), "status"]) == 1


def test_init_repository_binds_one_project(tmp_path: Path) -> None:
    root, db = _mini_repo(tmp_path)
    _write_template(root)
    result = init_repository("Alpha Pilot", repo_root=root, db_path=db)
    assert result.project["human_id"] == "PRJ-001"
    assert result.project["is_active"] == 1

    ident = load_repository_identity(root)
    assert ident.is_bound_project
    assert ident.project_human_id == "PRJ-001"
    assert ident.project_name == "Alpha Pilot"
    assert ident.isolation_model == "one-project-per-repository"
    assert ident.orchestration_scope == "project"
    assert ident.cross_project_access is False

    raw = json.loads((root / "project" / "repository.json").read_text(encoding="utf-8"))
    assert raw["repository_type"] == "delivery-project"
    assert raw["project_human_id"] == "PRJ-001"

    assert validate_project_isolation(db_path=db, repo_root=root).active_count == 1
    assert main(["--db", str(db), "--repo-root", str(root), "status"]) == 0


def test_cli_init_repository(tmp_path: Path) -> None:
    root, db = _mini_repo(tmp_path)
    _write_template(root)
    assert (
        main(
            [
                "--db",
                str(db),
                "--repo-root",
                str(root),
                "project",
                "init-repository",
                "--name",
                "CLI Bound",
            ]
        )
        == 0
    )
    ident = load_repository_identity(root)
    assert ident.project_human_id == "PRJ-001"


def test_second_initialization_fails(tmp_path: Path) -> None:
    root, db = _mini_repo(tmp_path)
    _write_template(root)
    init_repository("First", repo_root=root, db_path=db)
    with pytest.raises(store.StoreError, match="already bound"):
        init_repository("Second", repo_root=root, db_path=db)


def test_template_with_active_project_fails_init(tmp_path: Path) -> None:
    root, db = _mini_repo(tmp_path)
    _write_template(root)
    store.create_project("Leak", db_path=db, make_active=True, enforce_isolation=False)
    with pytest.raises(store.StoreError, match="active project"):
        init_repository("Should Fail", repo_root=root, db_path=db)


def test_template_prepare_requires_force(tmp_path: Path) -> None:
    root, db = _mini_repo(tmp_path)
    _write_bound(root)
    store.create_project("Bound", db_path=db, make_active=True, enforce_isolation=False)
    with pytest.raises(store.StoreError, match="--force"):
        prepare_template(force=False, repo_root=root, db_path=db)
    assert (
        main(
            [
                "--db",
                str(db),
                "--repo-root",
                str(root),
                "template",
                "prepare",
            ]
        )
        == 1
    )


def test_prepare_then_init_fresh_ids(tmp_path: Path) -> None:
    root, db = _mini_repo(tmp_path)
    _write_bound(root, human_id="PRJ-003", name="Old")
    # Simulate history then prepare
    store.create_project("Smoke", db_path=db, make_active=False)
    store.create_project("Smoke2", db_path=db, make_active=False)
    store.create_project("Old", db_path=db, make_active=True, enforce_isolation=False)
    (root / "product").mkdir()
    (root / "product" / "README.md").write_text("keep", encoding="utf-8")

    result = prepare_template(force=True, repo_root=root, db_path=db)
    assert "product" in result.reported_project_specific_paths
    assert (root / "product" / "README.md").is_file()  # not deleted

    ident = load_repository_identity(root)
    assert ident.is_template
    assert ident.project_human_id is None

    conn = connect(db)
    try:
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM projects WHERE is_active = 1"
        ).fetchone()["c"]
        total = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
    finally:
        conn.close()
    assert active == 0
    assert total == 0  # fresh DB reset — intentional new ID space

    bound = init_repository("New Delivery", repo_root=root, db_path=db)
    assert bound.project["human_id"] == "PRJ-001"
    assert load_repository_identity(root).project_human_id == "PRJ-001"


def test_init_writes_audit(tmp_path: Path) -> None:
    root, db = _mini_repo(tmp_path)
    _write_template(root)
    init_repository("Audited", repo_root=root, db_path=db)
    audits = store.list_audit(db_path=db, limit=20)
    actions = [a["action"] for a in audits]
    assert "repository.init-bind" in actions
    assert "create" in actions
