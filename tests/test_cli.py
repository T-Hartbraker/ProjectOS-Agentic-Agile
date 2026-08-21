"""CLI tests for python -m projectos registry commands."""

from __future__ import annotations

from pathlib import Path

from projectos.cli import main
from helpers import fake_status, init_git_repo, write_identity, write_registry


def test_registry_list_and_show(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    config = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-003",
                "repository_root": str(repo.resolve()),
                "enabled": True,
            }
        ],
    )
    assert main(["--config", str(config), "registry", "list"]) == 0
    out = capsys.readouterr().out
    assert "PRJ-003" in out
    assert "enabled" in out

    assert main(["--config", str(config), "registry", "show", "PRJ-003"]) == 0
    out = capsys.readouterr().out
    assert "project_human_id: PRJ-003" in out

    assert main(["--config", str(config), "registry", "show", "PRJ-404"]) == 1


def test_registry_validate_cli(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = init_git_repo(tmp_path / "repo")
    write_identity(repo, project_human_id="PRJ-003")
    config = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-003",
                "repository_root": str(repo),
                "enabled": True,
            }
        ],
    )

    from projectos import validation as validation_mod

    monkeypatch.setattr(
        validation_mod,
        "run_projectctl_status",
        lambda root: fake_status("PRJ-003"),
    )

    assert main(["--config", str(config), "registry", "validate"]) == 0
    out = capsys.readouterr().out
    assert "OK  PRJ-003" in out
