"""ProjectContext: registry-bound identity; client paths cannot override it."""

from __future__ import annotations

from pathlib import Path

import pytest

from projectos.errors import RegistryError, RepositoryValidationError
from projectos.project_context import ProjectContext, resolve_project_context
from projectos.services import ServiceContext, StatusService
from helpers import fake_status, init_git_repo, write_identity, write_registry


def _runner(human_id: str):
    return lambda root: fake_status(human_id)


def _register(
    tmp_path: Path,
    *projects: tuple[str, Path, bool],
) -> Path:
    rows = [
        {
            "project_human_id": human_id,
            "repository_root": str(repo.resolve()),
            "enabled": enabled,
        }
        for human_id, repo, enabled in projects
    ]
    return write_registry(tmp_path / "projects.json", rows)


def _bound_repo(tmp_path: Path, name: str, human_id: str) -> Path:
    repo = init_git_repo(tmp_path / name)
    write_identity(repo, project_human_id=human_id)
    (repo / "project-control").mkdir(parents=True, exist_ok=True)
    (repo / "project-control" / "project.db").write_bytes(b"sqlite")
    return repo


def test_resolve_from_project_human_id_only(tmp_path: Path) -> None:
    repo = _bound_repo(tmp_path, "alpha", "PRJ-A")
    config = _register(tmp_path, ("PRJ-A", repo, True))
    ctx = resolve_project_context(
        "PRJ-A",
        registry_path=config,
        projectctl_runner=_runner("PRJ-A"),
    )
    assert ctx.project_human_id == "PRJ-A"
    assert ctx.repository_root == repo.resolve()
    assert ctx.git_root == repo.resolve()
    assert ctx.identity.project_human_id == "PRJ-A"
    assert ctx.project_control_dir == (repo / "project-control").resolve()
    assert ctx.projectctl_db_path == (repo / "project-control" / "project.db").resolve()
    assert ctx.entry.enabled is True


def test_claimed_matching_root_is_accepted(tmp_path: Path) -> None:
    repo = _bound_repo(tmp_path, "alpha", "PRJ-A")
    config = _register(tmp_path, ("PRJ-A", repo, True))
    ctx = resolve_project_context(
        "PRJ-A",
        registry_path=config,
        claimed_repository_root=repo / "project-control" / ".." ,
        projectctl_runner=_runner("PRJ-A"),
    )
    assert ctx.repository_root == repo.resolve()


def test_malicious_claimed_root_cannot_override(tmp_path: Path) -> None:
    repo = _bound_repo(tmp_path, "alpha", "PRJ-A")
    evil = tmp_path / "evil"
    evil.mkdir()
    config = _register(tmp_path, ("PRJ-A", repo, True))
    with pytest.raises(RepositoryValidationError, match="cannot override ProjectContext"):
        resolve_project_context(
            "PRJ-A",
            registry_path=config,
            claimed_repository_root=evil,
            projectctl_runner=_runner("PRJ-A"),
        )


def test_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    repo = _bound_repo(tmp_path, "alpha", "PRJ-EVIL")
    config = _register(tmp_path, ("PRJ-A", repo, True))
    with pytest.raises(RepositoryValidationError, match="Identity mismatch"):
        resolve_project_context(
            "PRJ-A",
            registry_path=config,
            projectctl_runner=_runner("PRJ-A"),
        )


def test_two_projects_are_isolated_and_cannot_be_swapped(tmp_path: Path) -> None:
    repo_a = _bound_repo(tmp_path, "alpha", "PRJ-A")
    repo_b = _bound_repo(tmp_path, "bravo", "PRJ-B")
    config = _register(
        tmp_path,
        ("PRJ-A", repo_a, True),
        ("PRJ-B", repo_b, True),
    )
    ctx_a = resolve_project_context(
        "PRJ-A",
        registry_path=config,
        projectctl_runner=_runner("PRJ-A"),
    )
    ctx_b = resolve_project_context(
        "PRJ-B",
        registry_path=config,
        projectctl_runner=_runner("PRJ-B"),
    )
    assert ctx_a.repository_root == repo_a.resolve()
    assert ctx_b.repository_root == repo_b.resolve()
    assert ctx_a.project_control_dir != ctx_b.project_control_dir
    assert ctx_a.projectctl_db_path != ctx_b.projectctl_db_path

    with pytest.raises(RepositoryValidationError, match="cannot override ProjectContext"):
        resolve_project_context(
            "PRJ-A",
            registry_path=config,
            claimed_repository_root=repo_b,
            projectctl_runner=_runner("PRJ-A"),
        )
    with pytest.raises(RepositoryValidationError, match="cannot override ProjectContext"):
        ctx_a.assert_repository_root(repo_b)


def test_disabled_and_unknown_projects_are_rejected(tmp_path: Path) -> None:
    repo = _bound_repo(tmp_path, "alpha", "PRJ-A")
    config = _register(tmp_path, ("PRJ-A", repo, False))
    with pytest.raises(RepositoryValidationError, match="disabled"):
        resolve_project_context(
            "PRJ-A",
            registry_path=config,
            projectctl_runner=_runner("PRJ-A"),
        )
    enabled = _register(tmp_path, ("PRJ-A", repo, True))
    with pytest.raises(RegistryError, match="not in the registry"):
        resolve_project_context(
            "PRJ-Z",
            registry_path=enabled,
            projectctl_runner=_runner("PRJ-A"),
        )


def test_service_context_resolve_project_and_status_ignore_claimed_path(
    tmp_path: Path,
) -> None:
    repo_a = _bound_repo(tmp_path, "alpha", "PRJ-A")
    repo_b = _bound_repo(tmp_path, "bravo", "PRJ-B")
    config = _register(
        tmp_path,
        ("PRJ-A", repo_a, True),
        ("PRJ-B", repo_b, True),
    )
    svc_ctx = ServiceContext(db_path=tmp_path / "projectos.db", registry_path=config)
    project = svc_ctx.resolve_project("PRJ-A", projectctl_runner=_runner("PRJ-A"))
    assert isinstance(project, ProjectContext)
    assert project.git_root == repo_a.resolve()

    with pytest.raises(RepositoryValidationError, match="cannot override"):
        StatusService(svc_ctx).delivery(
            "PRJ-A",
            claimed_repository_root=repo_b,
            projectctl_runner=_runner("PRJ-A"),
        )
