"""Governed project onboarding: register, update, and disable registry entries.

The registry is mutated only through this module. A candidate is fully
validated in memory; persistence is a single atomic replace of projects.json.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from projectos.errors import RegistryConflictError, RegistryError, RepositoryValidationError
from projectos.gitutil import resolve_git_root
from projectos.paths import DEFAULT_REGISTRY_PATH
from projectos.project_context import PROJECT_CONTROL_DIRNAME
from projectos.registry import (
    ProjectRegistry,
    RegistryEntry,
    load_registry,
    load_registry_or_empty,
    persist_registry,
    replace_entry,
)
from projectos.repository import (
    REPOSITORY_TYPE_DELIVERY_PROJECT,
    RepositoryIdentity,
    load_repository_identity,
)
from projectos.validation import ValidatedProject, validate_registry_entry


@dataclass(frozen=True)
class OnboardingResult:
    action: str
    entry: RegistryEntry
    git_root: Path
    identity: RepositoryIdentity
    project_control_dir: Path
    validated: ValidatedProject | None = None


def _as_git_root(repository_path: Path | str) -> Path:
    path = Path(repository_path)
    if path.is_file():
        path = path.parent
    if not path.exists():
        raise RepositoryValidationError(
            f"repository path does not exist: {Path(repository_path).resolve()}"
        )
    return resolve_git_root(path)


def inspect_repository(
    repository_path: Path | str,
    *,
    projectctl_runner=None,
) -> tuple[Path, RepositoryIdentity]:
    """Discover git root and validate delivery-project identity (no persist)."""
    git_root = _as_git_root(repository_path)
    identity = load_repository_identity(git_root)
    if identity.is_template or identity.repository_type != REPOSITORY_TYPE_DELIVERY_PROJECT:
        raise RepositoryValidationError(
            f"onboarding requires repository_type {REPOSITORY_TYPE_DELIVERY_PROJECT!r} "
            f"(got {identity.repository_type!r}) at {git_root}"
        )
    if not identity.project_human_id or not str(identity.project_human_id).strip():
        raise RepositoryValidationError(
            f"delivery-project missing project_human_id at {identity.path}"
        )
    if not identity.project_name or not str(identity.project_name).strip():
        raise RepositoryValidationError(
            f"delivery-project missing project_name at {identity.path}"
        )
    return git_root, identity


def _candidate_entry(
    identity: RepositoryIdentity,
    git_root: Path,
    *,
    enabled: bool = True,
) -> RegistryEntry:
    human_id = str(identity.project_human_id).strip()
    root = Path(git_root).resolve()
    raw = {
        "project_human_id": human_id,
        "repository_root": str(root),
        "enabled": enabled,
    }
    return RegistryEntry(
        project_human_id=human_id,
        repository_root=root,
        enabled=enabled,
        raw=raw,
    )


def _root_key(root: Path | str) -> str:
    return str(Path(root).resolve()).casefold()


def _assert_no_conflicts(
    registry: ProjectRegistry,
    *,
    human_id: str,
    git_root: Path,
    ignore_human_id: str | None = None,
) -> None:
    root_key = _root_key(git_root)
    for entry in registry.projects:
        if ignore_human_id and entry.project_human_id == ignore_human_id:
            continue
        if entry.project_human_id == human_id:
            raise RegistryConflictError(
                f"project_human_id {human_id!r} is already registered at "
                f"{entry.repository_root}"
            )
        if _root_key(entry.repository_root) == root_key:
            raise RegistryConflictError(
                f"repository_root {git_root} is already registered as "
                f"{entry.project_human_id}"
            )


def _validate_candidate(
    entry: RegistryEntry,
    *,
    projectctl_runner=None,
) -> ValidatedProject:
    return validate_registry_entry(entry, projectctl_runner=projectctl_runner)


def register_project(
    repository_path: Path | str,
    *,
    registry_path: Path | str | None = None,
    projectctl_runner=None,
) -> OnboardingResult:
    """Onboard a delivery repository. Does not write until validation succeeds."""
    path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    git_root, identity = inspect_repository(
        repository_path, projectctl_runner=projectctl_runner
    )
    human_id = str(identity.project_human_id)
    registry = load_registry_or_empty(path)
    existing = registry.get(human_id)
    if existing is not None and _root_key(existing.repository_root) == _root_key(git_root):
        raise RegistryConflictError(
            f"project {human_id!r} is already registered; use registry update"
        )
    _assert_no_conflicts(registry, human_id=human_id, git_root=git_root)
    candidate = _candidate_entry(identity, git_root, enabled=True)
    validated = _validate_candidate(candidate, projectctl_runner=projectctl_runner)
    next_registry = replace_entry(registry, candidate)
    persist_registry(next_registry)
    return OnboardingResult(
        action="registered",
        entry=candidate,
        git_root=validated.git_root,
        identity=validated.identity,
        project_control_dir=validated.git_root / PROJECT_CONTROL_DIRNAME,
        validated=validated,
    )


def update_project(
    project_human_id: str,
    *,
    repository_path: Path | str | None = None,
    registry_path: Path | str | None = None,
    projectctl_runner=None,
) -> OnboardingResult:
    """Re-validate and refresh an existing registry row."""
    path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    registry = load_registry(path)
    human_id = str(project_human_id).strip()
    existing = registry.get(human_id)
    if existing is None:
        raise RegistryError(f"Project {human_id!r} is not in the registry")

    source = repository_path if repository_path is not None else existing.repository_root
    git_root, identity = inspect_repository(
        source, projectctl_runner=projectctl_runner
    )
    discovered_id = str(identity.project_human_id)
    if discovered_id != human_id:
        raise RepositoryValidationError(
            f"update {human_id!r} refused: repository identity is {discovered_id!r}"
        )
    _assert_no_conflicts(
        registry,
        human_id=human_id,
        git_root=git_root,
        ignore_human_id=human_id,
    )
    candidate = _candidate_entry(identity, git_root, enabled=True)
    validated = _validate_candidate(candidate, projectctl_runner=projectctl_runner)
    persist_registry(replace_entry(registry, candidate))
    return OnboardingResult(
        action="updated",
        entry=candidate,
        git_root=validated.git_root,
        identity=validated.identity,
        project_control_dir=validated.git_root / PROJECT_CONTROL_DIRNAME,
        validated=validated,
    )


def disable_project(
    project_human_id: str,
    *,
    registry_path: Path | str | None = None,
) -> OnboardingResult:
    """Disable a registered project. Does not delete history."""
    path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    registry = load_registry(path)
    human_id = str(project_human_id).strip()
    existing = registry.get(human_id)
    if existing is None:
        raise RegistryError(f"Project {human_id!r} is not in the registry")
    disabled = replace(existing, enabled=False, raw={**existing.raw, "enabled": False})
    persist_registry(replace_entry(registry, disabled))
    git_root = Path(disabled.repository_root).resolve()
    try:
        identity = load_repository_identity(git_root)
    except Exception:
        identity = RepositoryIdentity(
            schema_version=1,
            repository_type=REPOSITORY_TYPE_DELIVERY_PROJECT,
            project_human_id=human_id,
            isolation_model="one-project-per-repository",
            project_name=None,
        )
    return OnboardingResult(
        action="disabled",
        entry=disabled,
        git_root=git_root,
        identity=identity,
        project_control_dir=git_root / PROJECT_CONTROL_DIRNAME,
    )
