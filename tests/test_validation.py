"""Unit tests for repository validation mismatch cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from projectos.errors import (
    GitRepositoryError,
    PathBoundaryError,
    ProjectctlError,
    RepositoryValidationError,
)
from projectos.gitutil import assert_within_git_root, resolve_git_root
from projectos.projectctl_bridge import ProjectctlStatusResult, parse_active_project_human_id
from projectos.registry import RegistryEntry
from projectos.validation import validate_registry, validate_registry_entry
from helpers import (
    fake_status,
    init_git_repo,
    schema_path,
    write_identity,
    write_registry,
)


def _entry(repo: Path, human_id: str = "PRJ-003", *, enabled: bool = True) -> RegistryEntry:
    return RegistryEntry(
        project_human_id=human_id,
        repository_root=repo.resolve(),
        enabled=enabled,
        raw={
            "project_human_id": human_id,
            "repository_root": str(repo.resolve()),
            "enabled": enabled,
        },
    )


def test_valid_entry_passes(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    write_identity(repo, project_human_id="PRJ-003")
    result = validate_registry_entry(
        _entry(repo),
        projectctl_runner=lambda root: fake_status("PRJ-003"),
    )
    assert result.active_project_human_id == "PRJ-003"
    assert result.git_root == repo.resolve()


def test_missing_git_repository() -> None:
    import tempfile
    import uuid

    base = Path(tempfile.gettempdir()) / f"projectos-val-{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    repo = base / "not-a-git-repo"
    repo.mkdir()
    write_identity(repo, project_human_id="PRJ-003")
    with pytest.raises(GitRepositoryError, match="No Git repository"):
        validate_registry_entry(
            _entry(repo),
            projectctl_runner=lambda root: fake_status("PRJ-003"),
        )


def test_missing_repository_json(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    with pytest.raises(RepositoryValidationError, match="missing"):
        validate_registry_entry(
            _entry(repo),
            projectctl_runner=lambda root: fake_status("PRJ-003"),
        )


def test_malformed_repository_json(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    write_identity(repo, corrupt=True)
    with pytest.raises(RepositoryValidationError, match="malformed JSON"):
        validate_registry_entry(
            _entry(repo),
            projectctl_runner=lambda root: fake_status("PRJ-003"),
        )


def test_unbound_template_rejected(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    write_identity(
        repo,
        project_human_id=None,
        project_name=None,
        repository_type="delivery-template",
    )
    with pytest.raises(RepositoryValidationError, match="unbound delivery-template"):
        validate_registry_entry(
            _entry(repo),
            projectctl_runner=lambda root: fake_status("PRJ-003"),
        )


def test_identity_mismatch_never_autorepairs(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    write_identity(repo, project_human_id="PRJ-999")
    with pytest.raises(RepositoryValidationError, match="Identity mismatch"):
        validate_registry_entry(
            _entry(repo, "PRJ-003"),
            projectctl_runner=lambda root: fake_status("PRJ-003"),
        )


def test_wrong_repository_type(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    write_identity(
        repo,
        project_human_id="PRJ-003",
        overrides={"repository_type": "something-else"},
    )
    with pytest.raises(RepositoryValidationError, match="repository_type"):
        validate_registry_entry(
            _entry(repo),
            projectctl_runner=lambda root: fake_status("PRJ-003"),
        )


def test_path_outside_git_root_rejected(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    outside = (tmp_path / "outside.txt").resolve()
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(PathBoundaryError, match="outside repository Git root"):
        assert_within_git_root(outside, resolve_git_root(repo))


def test_registered_root_not_git_root_rejected(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    nested = repo / "nested"
    nested.mkdir()
    write_identity(nested, project_human_id="PRJ-003")
    with pytest.raises(RepositoryValidationError, match="not the Git root"):
        validate_registry_entry(
            _entry(nested),
            projectctl_runner=lambda root: fake_status("PRJ-003"),
        )


def test_projectctl_status_failure(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    write_identity(repo, project_human_id="PRJ-003")

    def failing(_root: Path) -> ProjectctlStatusResult:
        return ProjectctlStatusResult(
            returncode=1,
            stdout="",
            stderr="error: no active project",
            active_project_human_id=None,
            python_executable=Path("/fake/python"),
        )

    with pytest.raises(ProjectctlError, match="projectctl status failed"):
        validate_registry_entry(_entry(repo), projectctl_runner=failing)


def test_projectctl_active_id_mismatch(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    write_identity(repo, project_human_id="PRJ-003")
    with pytest.raises(ProjectctlError, match="does not match expected"):
        validate_registry_entry(
            _entry(repo),
            projectctl_runner=lambda root: fake_status("PRJ-001"),
        )


def test_parse_active_project_human_id() -> None:
    text = "Active project: PRJ-003 - Personal Task Manager Pilot\nStatus: active\n"
    assert parse_active_project_human_id(text) == "PRJ-003"
    assert parse_active_project_human_id("No active project.\n") is None


def test_validate_registry_collects_issues(tmp_path: Path) -> None:
    good = init_git_repo(tmp_path / "good")
    write_identity(good, project_human_id="PRJ-001")
    bad = init_git_repo(tmp_path / "bad")
    write_identity(bad, project_human_id="PRJ-999")

    config = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-001",
                "repository_root": str(good),
                "enabled": True,
            },
            {
                "project_human_id": "PRJ-002",
                "repository_root": str(bad),
                "enabled": True,
            },
        ],
    )

    def runner(root: Path) -> ProjectctlStatusResult:
        if root.resolve() == good.resolve():
            return fake_status("PRJ-001")
        return fake_status("PRJ-002")

    report = validate_registry(
        path=config,
        projectctl_runner=runner,
    )
    assert not report.ok
    assert len(report.validated) == 1
    assert report.validated[0].entry.project_human_id == "PRJ-001"
    assert len(report.issues) == 1
    assert report.issues[0].project_human_id == "PRJ-002"
    assert "Identity mismatch" in report.issues[0].error


def test_schema_path_exists() -> None:
    assert schema_path().is_file()
