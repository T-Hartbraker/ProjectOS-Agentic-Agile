"""Trusted project identity resolved only from project_human_id.

Client-supplied repository paths are never a source of truth. They may be
provided only as a claimed value that must match the registry-derived root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from projectos.errors import RegistryError, RepositoryValidationError
from projectos.gitutil import assert_within_git_root
from projectos.registry import RegistryEntry, load_registry
from projectos.repository import RepositoryIdentity
from projectos.validation import ValidatedProject, validate_registry_entry

PROJECT_CONTROL_DIRNAME = "project-control"
PROJECTCTL_DB_NAME = "project.db"


def _paths_equal(left: Path | str, right: Path | str) -> bool:
    return Path(left).resolve() == Path(right).resolve()


@dataclass(frozen=True)
class ProjectContext:
    """Authoritative, fail-closed view of one enabled delivery project."""

    project_human_id: str
    entry: RegistryEntry
    repository_root: Path
    git_root: Path
    identity: RepositoryIdentity
    project_control_dir: Path
    projectctl_db_path: Path
    active_project_human_id: str
    projectctl_python: Path

    def to_validated_project(self) -> ValidatedProject:
        return ValidatedProject(
            entry=self.entry,
            git_root=self.git_root,
            identity=self.identity,
            active_project_human_id=self.active_project_human_id,
            projectctl_python=self.projectctl_python,
        )

    def assert_repository_root(self, claimed: Path | str) -> Path:
        """Reject a client-supplied root that does not match this context."""
        resolved = Path(claimed).resolve()
        if not _paths_equal(resolved, self.repository_root):
            raise RepositoryValidationError(
                "client-supplied repository path cannot override ProjectContext "
                f"for {self.project_human_id}: claimed {resolved}, "
                f"registry-derived {self.repository_root}"
            )
        return resolved


def _project_control_paths(git_root: Path) -> tuple[Path, Path]:
    root = Path(git_root).resolve()
    control_dir = assert_within_git_root(root / PROJECT_CONTROL_DIRNAME, root)
    db_path = assert_within_git_root(control_dir / PROJECTCTL_DB_NAME, root)
    return control_dir, db_path


def resolve_project_context(
    project_human_id: str,
    *,
    registry_path: Path | str | None = None,
    claimed_repository_root: Path | str | None = None,
    projectctl_runner=None,
) -> ProjectContext:
    """Resolve an enabled project solely from project_human_id.

    ``claimed_repository_root`` is an optional assertion. It is never used to
    select the repository; a mismatch fails closed.
    """
    requested = str(project_human_id or "").strip()
    if not requested:
        raise RegistryError("project_human_id is required")

    registry = load_registry(registry_path)
    entry = registry.get(requested)
    if entry is None:
        raise RegistryError(f"Project {requested!r} is not in the registry")
    if not entry.enabled:
        raise RepositoryValidationError(
            f"Project {requested} is disabled in the registry"
        )

    validated = validate_registry_entry(entry, projectctl_runner=projectctl_runner)
    repository_root = Path(entry.repository_root).resolve()
    git_root = Path(validated.git_root).resolve()
    if repository_root != git_root:
        raise RepositoryValidationError(
            f"Registered repository_root {repository_root} is not the Git root "
            f"{git_root}. Refusing to continue; never auto-repair."
        )

    control_dir, db_path = _project_control_paths(git_root)
    context = ProjectContext(
        project_human_id=requested,
        entry=entry,
        repository_root=repository_root,
        git_root=git_root,
        identity=validated.identity,
        project_control_dir=control_dir,
        projectctl_db_path=db_path,
        active_project_human_id=validated.active_project_human_id,
        projectctl_python=validated.projectctl_python,
    )
    if claimed_repository_root is not None:
        context.assert_repository_root(claimed_repository_root)
    return context
