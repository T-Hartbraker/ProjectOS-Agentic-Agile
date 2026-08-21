"""Typed errors for ProjectOS registry and validation."""

from __future__ import annotations


class ProjectOSError(Exception):
    """Base error for ProjectOS failures."""


class RegistryError(ProjectOSError):
    """projects.json missing, malformed, or schema-invalid."""


class RegistryConflictError(RegistryError):
    """Duplicate project IDs or repository roots in the registry."""


class RepositoryValidationError(ProjectOSError):
    """A registered repository failed identity or binding checks."""


class GitRepositoryError(RepositoryValidationError):
    """Missing or unusable Git repository for a registered root."""


class PathBoundaryError(RepositoryValidationError):
    """A path falls outside the repository Git root."""


class ProjectctlError(RepositoryValidationError):
    """Invoking or interpreting projectctl status failed."""


class OrchestrationError(ProjectOSError):
    """Orchestration state or job lifecycle failure."""


class LeaseError(OrchestrationError):
    """Lease acquisition, renewal, or release failed."""


class WorktreeError(OrchestrationError):
    """Worktree create/associate/collision failure."""


class CursorAdapterError(OrchestrationError):
    """Cursor Agent adapter invocation failure."""


class WorkerError(OrchestrationError):
    """Worker runtime failure."""
