"""Governed registry onboarding: register, update, disable, atomic persist."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from projectos.cli import main
from projectos.errors import RegistryConflictError, RegistryError, RepositoryValidationError
from projectos.onboarding import disable_project, register_project, update_project
from projectos.registry import load_registry
from projectos.services import RegistryService, ServiceContext
from helpers import fake_status, init_git_repo, write_identity, write_registry


def _runner(human_id: str):
    return lambda root: fake_status(human_id)


def _delivery_repo(tmp_path: Path, name: str, human_id: str, *, project_name: str = "Example") -> Path:
    repo = init_git_repo(tmp_path / name)
    write_identity(repo, project_human_id=human_id, project_name=project_name)
    nested = repo / "src"
    nested.mkdir(exist_ok=True)
    return repo


def test_register_discovers_git_root_and_persists(tmp_path: Path) -> None:
    repo = _delivery_repo(tmp_path, "alpha", "PRJ-A")
    registry_path = tmp_path / "projects.json"
    result = register_project(
        repo / "src",
        registry_path=registry_path,
        projectctl_runner=_runner("PRJ-A"),
    )
    assert result.action == "registered"
    assert result.entry.project_human_id == "PRJ-A"
    assert result.git_root == repo.resolve()
    assert result.entry.repository_root == repo.resolve()
    assert result.entry.enabled is True
    loaded = load_registry(registry_path)
    assert len(loaded.projects) == 1
    assert loaded.projects[0].project_human_id == "PRJ-A"
    assert Path(loaded.projects[0].repository_root).resolve() == repo.resolve()


def test_register_conflict_same_id_or_root(tmp_path: Path) -> None:
    repo_a = _delivery_repo(tmp_path, "alpha", "PRJ-A")
    repo_b = _delivery_repo(tmp_path, "bravo", "PRJ-B")
    registry_path = tmp_path / "projects.json"
    register_project(repo_a, registry_path=registry_path, projectctl_runner=_runner("PRJ-A"))
    with pytest.raises(RegistryConflictError, match="already registered"):
        register_project(repo_a, registry_path=registry_path, projectctl_runner=_runner("PRJ-A"))
    write_identity(repo_b, project_human_id="PRJ-A", project_name="Clone")
    with pytest.raises(RegistryConflictError, match="already registered"):
        register_project(repo_b, registry_path=registry_path, projectctl_runner=_runner("PRJ-A"))
    write_identity(repo_b, project_human_id="PRJ-B", project_name="Bravo")
    # Same root under a second id: reuse repo_a by registering a different id from a copy identity at same root
    write_identity(repo_a, project_human_id="PRJ-X", project_name="Hijack")
    with pytest.raises(RegistryConflictError, match="already registered as"):
        register_project(repo_a, registry_path=registry_path, projectctl_runner=_runner("PRJ-X"))


def test_failed_validation_does_not_mutate_registry(tmp_path: Path) -> None:
    existing = _delivery_repo(tmp_path, "keep", "PRJ-KEEP")
    registry_path = tmp_path / "projects.json"
    register_project(
        existing, registry_path=registry_path, projectctl_runner=_runner("PRJ-KEEP")
    )
    before = registry_path.read_text(encoding="utf-8")
    bad = init_git_repo(tmp_path / "bad")
    write_identity(bad, project_human_id="PRJ-BAD", project_name="Bad")

    def failing_status(root):
        return fake_status("PRJ-OTHER")

    with pytest.raises(RepositoryValidationError):
        register_project(bad, registry_path=registry_path, projectctl_runner=failing_status)
    assert registry_path.read_text(encoding="utf-8") == before
    loaded = load_registry(registry_path)
    assert [p.project_human_id for p in loaded.projects] == ["PRJ-KEEP"]


def test_failed_atomic_replace_does_not_partially_mutate(tmp_path: Path, monkeypatch) -> None:
    repo = _delivery_repo(tmp_path, "alpha", "PRJ-A")
    registry_path = write_registry(tmp_path / "projects.json", [])
    before = registry_path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("projectos.registry.os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        register_project(repo, registry_path=registry_path, projectctl_runner=_runner("PRJ-A"))
    assert registry_path.read_text(encoding="utf-8") == before
    leftover = list(registry_path.parent.glob(".projects-*.tmp"))
    assert leftover == []


def test_template_and_missing_name_rejected(tmp_path: Path) -> None:
    registry_path = tmp_path / "projects.json"
    template = init_git_repo(tmp_path / "tmpl")
    write_identity(
        template,
        project_human_id=None,
        project_name=None,
        repository_type="delivery-template",
    )
    with pytest.raises(RepositoryValidationError, match="repository_type"):
        register_project(template, registry_path=registry_path, projectctl_runner=_runner("PRJ-A"))
    assert not registry_path.exists()

    unnamed = init_git_repo(tmp_path / "unnamed")
    write_identity(unnamed, project_human_id="PRJ-N", project_name="  ")
    with pytest.raises(RepositoryValidationError, match="project_name"):
        register_project(unnamed, registry_path=registry_path, projectctl_runner=_runner("PRJ-N"))
    assert not registry_path.exists()


def test_update_and_disable(tmp_path: Path) -> None:
    repo = _delivery_repo(tmp_path, "alpha", "PRJ-A")
    registry_path = tmp_path / "projects.json"
    register_project(repo, registry_path=registry_path, projectctl_runner=_runner("PRJ-A"))
    moved = _delivery_repo(tmp_path, "alpha-moved", "PRJ-A")
    updated = update_project(
        "PRJ-A",
        repository_path=moved,
        registry_path=registry_path,
        projectctl_runner=_runner("PRJ-A"),
    )
    assert updated.action == "updated"
    assert updated.entry.repository_root == moved.resolve()
    loaded = load_registry(registry_path)
    assert Path(loaded.get("PRJ-A").repository_root).resolve() == moved.resolve()
    assert loaded.get("PRJ-A").enabled is True

    disabled = disable_project("PRJ-A", registry_path=registry_path)
    assert disabled.action == "disabled"
    assert disabled.entry.enabled is False
    loaded = load_registry(registry_path)
    assert loaded.get("PRJ-A").enabled is False
    with pytest.raises(RegistryError, match="not in the registry"):
        disable_project("PRJ-MISSING", registry_path=registry_path)


def test_cli_register_update_disable(tmp_path: Path, monkeypatch) -> None:
    repo = _delivery_repo(tmp_path, "alpha", "PRJ-A")
    registry_path = tmp_path / "projects.json"
    from projectos import validation as validation_mod

    monkeypatch.setattr(validation_mod, "run_projectctl_status", _runner("PRJ-A"))

    assert (
        main(
            [
                "--config",
                str(registry_path),
                "registry",
                "register",
                str(repo),
            ]
        )
        == 0
    )
    loaded = load_registry(registry_path)
    assert loaded.get("PRJ-A") is not None
    assert (
        main(
            [
                "--config",
                str(registry_path),
                "registry",
                "disable",
                "PRJ-A",
            ]
        )
        == 0
    )
    assert load_registry(registry_path).get("PRJ-A").enabled is False
    assert (
        main(
            [
                "--config",
                str(registry_path),
                "registry",
                "update",
                "PRJ-A",
            ]
        )
        == 0
    )
    assert load_registry(registry_path).get("PRJ-A").enabled is True
