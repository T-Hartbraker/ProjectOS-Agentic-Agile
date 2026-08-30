"""CLI smoke-oriented tests."""

from __future__ import annotations

import json
from pathlib import Path

from projectctl.cli import main
from projectctl.migrate import initialize_database
from projectctl import store


def _repo_with_identity(tmp_path: Path, project_human_id: str = "PRJ-001") -> Path:
    root = tmp_path / "repo"
    (root / "project").mkdir(parents=True)
    (root / "project" / "repository.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository_type": "delivery-project",
                "project_human_id": project_human_id,
                "project_name": "Test",
                "isolation_model": "one-project-per-repository",
            }
        ),
        encoding="utf-8",
    )
    return root


def test_cli_help_succeeds() -> None:
    assert main(["--help"]) == 0


def test_cli_subcommand_help_succeeds() -> None:
    for group in (
        "project",
        "requirement",
        "story",
        "defect",
        "risk",
        "assumption",
        "decision",
        "iteration",
        "release",
        "trace",
        "audit",
        "customfield",
        "template",
    ):
        # argparse --help on a parent with required subparsers exits 0 via SystemExit
        try:
            code = main([group, "--help"])
            assert code == 0
        except SystemExit as exc:
            assert exc.code == 0


def test_cli_status_no_project_fails_isolation(tmp_path: Path) -> None:
    """Zero active projects is a closed failure for project-scoped status."""
    db = tmp_path / "cli.db"
    initialize_database(db_path=db)
    repo = _repo_with_identity(tmp_path, "PRJ-001")
    code = main(["--db", str(db), "--repo-root", str(repo), "status"])
    assert code == 1


def test_cli_project_workflow(tmp_path: Path, capsys) -> None:
    db = tmp_path / "cli.db"
    repo = _repo_with_identity(tmp_path, "PRJ-001")
    assert main(["--db", str(db), "init"]) == 0
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
                "CLI Project",
            ]
        )
        == 0
    )
    assert main(["--db", str(db), "--repo-root", str(repo), "project", "list"]) == 0
    assert (
        main(
            ["--db", str(db), "--repo-root", str(repo), "project", "show", "PRJ-001"]
        )
        == 0
    )
    assert main(["--db", str(db), "--repo-root", str(repo), "status"]) == 0
    assert main(["--db", str(db), "audit", "show"]) == 0
    out = capsys.readouterr().out
    assert "PRJ-001" in out
    assert "CLI Project" in out


def test_cli_groups_create_minimum(tmp_path: Path) -> None:
    db = tmp_path / "cli2.db"
    repo = _repo_with_identity(tmp_path, "PRJ-001")
    initialize_database(db_path=db)
    store.create_project("Workflow", db_path=db)
    base = ["--db", str(db), "--repo-root", str(repo)]
    assert main([*base, "requirement", "create", "--title", "R1"]) == 0
    assert main([*base, "story", "create", "--title", "S1"]) == 0
    assert main([*base, "defect", "create", "--title", "D1"]) == 0
    assert main([*base, "risk", "create", "--title", "Risk1"]) == 0
    assert main([*base, "assumption", "create", "--statement", "Users exist"]) == 0
    assert (
        main(
            [
                *base,
                "decision",
                "create",
                "--title",
                "Use SQLite",
                "--decision",
                "Approved",
            ]
        )
        == 0
    )
    assert main([*base, "iteration", "create", "--name", "Iter 1"]) == 0
    assert main([*base, "release", "create", "--name", "R1.0"]) == 0
    assert (
        main(
            [
                *base,
                "trace",
                "create",
                "--source-type",
                "requirement",
                "--source-id",
                "REQ-001",
                "--link-type",
                "DECOMPOSED_INTO",
                "--target-type",
                "story",
                "--target-id",
                "US-001",
            ]
        )
        == 0
    )
