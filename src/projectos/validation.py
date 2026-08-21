"""Validate registered delivery repositories against identity and projectctl."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from projectos.errors import RepositoryValidationError
from projectos.gitutil import assert_within_git_root, resolve_git_root
from projectos.projectctl_bridge import (
    ensure_single_active_project,
    run_projectctl_status,
)
from projectos.registry import ProjectRegistry, RegistryEntry, load_registry
from projectos.repository import (
    REPOSITORY_TYPE_DELIVERY_PROJECT,
    RepositoryIdentity,
    load_repository_identity,
    repository_json_path,
)


@dataclass(frozen=True)
class ValidatedProject:
    entry: RegistryEntry
    git_root: Path
    identity: RepositoryIdentity
    active_project_human_id: str
    projectctl_python: Path


@dataclass(frozen=True)
class ValidationIssue:
    project_human_id: str | None
    repository_root: Path | None
    error: str


@dataclass(frozen=True)
class ValidationReport:
    validated: tuple[ValidatedProject, ...]
    issues: tuple[ValidationIssue, ...]

    @property
    def ok(self) -> bool:
        return len(self.issues) == 0


def validate_registry_entry(
    entry: RegistryEntry,
    *,
    projectctl_runner=None,
) -> ValidatedProject:
    """Validate one enabled registry entry. Never auto-repairs mismatches."""
    runner = projectctl_runner or run_projectctl_status
    if not entry.enabled:
        raise RepositoryValidationError(
            f"Registry entry {entry.project_human_id} is disabled"
        )

    repo_root = entry.repository_root
    if not repo_root.is_absolute():
        raise RepositoryValidationError(
            f"repository_root must be absolute for {entry.project_human_id}: "
            f"{repo_root}"
        )

    git_root = resolve_git_root(repo_root)
    # Registered root must itself lie within the Git boundary.
    resolved_root = assert_within_git_root(repo_root, git_root)
    # project/repository.json is project-scoped work — must stay inside Git root.
    assert_within_git_root(repository_json_path(resolved_root), git_root)

    if resolved_root != git_root:
        raise RepositoryValidationError(
            f"Registered repository_root {resolved_root} is not the Git root "
            f"{git_root}. Refusing to continue; never auto-repair."
        )

    identity = load_repository_identity(resolved_root)

    if identity.is_template:
        raise RepositoryValidationError(
            f"Repository at {resolved_root} is an unbound delivery-template "
            f"(no project_human_id). ProjectOS only manages delivery-project "
            "repositories."
        )

    if identity.repository_type != REPOSITORY_TYPE_DELIVERY_PROJECT:
        raise RepositoryValidationError(
            f"repository_type must be {REPOSITORY_TYPE_DELIVERY_PROJECT!r} "
            f"(got {identity.repository_type!r}) at {identity.path}"
        )

    if not identity.project_human_id:
        raise RepositoryValidationError(
            f"delivery-project identity missing project_human_id at {identity.path}"
        )

    if identity.project_human_id != entry.project_human_id:
        raise RepositoryValidationError(
            f"Identity mismatch: registry has {entry.project_human_id}, "
            f"repository.json has {identity.project_human_id} "
            f"(from {identity.path}). Refusing to continue; never auto-repair."
        )

    status = runner(resolved_root)
    active_id = ensure_single_active_project(
        status,
        expected_human_id=entry.project_human_id,
    )

    return ValidatedProject(
        entry=entry,
        git_root=git_root,
        identity=identity,
        active_project_human_id=active_id,
        projectctl_python=status.python_executable,
    )


def validate_registry(
    registry: ProjectRegistry | None = None,
    *,
    path: Path | str | None = None,
    project_human_id: str | None = None,
    projectctl_runner=None,
) -> ValidationReport:
    """Validate enabled registry entries (or one ID). Collect all issues."""
    runner = projectctl_runner or run_projectctl_status
    reg = registry if registry is not None else load_registry(path)

    if project_human_id is not None:
        entry = reg.get(project_human_id)
        if entry is None:
            return ValidationReport(
                validated=(),
                issues=(
                    ValidationIssue(
                        project_human_id=project_human_id,
                        repository_root=None,
                        error=f"Project {project_human_id!r} is not in the registry",
                    ),
                ),
            )
        targets = (entry,)
    else:
        targets = reg.enabled_projects()

    validated: list[ValidatedProject] = []
    issues: list[ValidationIssue] = []
    for entry in targets:
        if not entry.enabled:
            issues.append(
                ValidationIssue(
                    project_human_id=entry.project_human_id,
                    repository_root=entry.repository_root,
                    error="Project is disabled in the registry",
                )
            )
            continue
        try:
            validated.append(
                validate_registry_entry(
                    entry,
                    projectctl_runner=runner,
                )
            )
        except Exception as exc:  # noqa: BLE001 — collect per-project failures
            issues.append(
                ValidationIssue(
                    project_human_id=entry.project_human_id,
                    repository_root=entry.repository_root,
                    error=str(exc),
                )
            )

    return ValidationReport(
        validated=tuple(validated),
        issues=tuple(issues),
    )
