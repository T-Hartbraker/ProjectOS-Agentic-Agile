"""Shared paths for application services (CLI and future HTTP adapters)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.project_context import ProjectContext, resolve_project_context


@dataclass(frozen=True)
class ServiceContext:
    """Request-scoped paths. Construct once per CLI invocation or API call."""

    db_path: Path = DEFAULT_DB_PATH
    registry_path: Path = DEFAULT_REGISTRY_PATH

    @classmethod
    def from_cli_args(cls, args) -> ServiceContext:
        return cls.from_args(args)

    @classmethod
    def from_args(cls, args) -> ServiceContext:
        db = getattr(args, "db", None)
        registry = getattr(args, "config", None)
        return cls(
            db_path=Path(db) if db is not None else DEFAULT_DB_PATH,
            registry_path=Path(registry) if registry is not None else DEFAULT_REGISTRY_PATH,
        )

    def resolve_project(
        self,
        project_human_id: str,
        *,
        claimed_repository_root: Path | str | None = None,
        projectctl_runner=None,
    ) -> ProjectContext:
        """Resolve a trusted ProjectContext from project_human_id only."""
        return resolve_project_context(
            project_human_id,
            registry_path=self.registry_path,
            claimed_repository_root=claimed_repository_root,
            projectctl_runner=projectctl_runner,
        )
