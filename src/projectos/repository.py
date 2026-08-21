"""Load and structurally validate project/repository.json identity."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from projectos.errors import RepositoryValidationError

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})
REPOSITORY_TYPE_DELIVERY_PROJECT = "delivery-project"
REPOSITORY_TYPE_DELIVERY_TEMPLATE = "delivery-template"
SUPPORTED_REPOSITORY_TYPES = frozenset(
    {REPOSITORY_TYPE_DELIVERY_PROJECT, REPOSITORY_TYPE_DELIVERY_TEMPLATE}
)
REQUIRED_ISOLATION_MODEL = "one-project-per-repository"


@dataclass(frozen=True)
class RepositoryIdentity:
    schema_version: int
    repository_type: str
    project_human_id: str | None
    isolation_model: str
    project_name: str | None = None
    orchestration_scope: str | None = None
    cross_project_access: bool | None = None
    path: Path | None = None
    raw: dict[str, Any] | None = None

    @property
    def is_template(self) -> bool:
        return self.repository_type == REPOSITORY_TYPE_DELIVERY_TEMPLATE

    @property
    def is_bound_project(self) -> bool:
        return self.repository_type == REPOSITORY_TYPE_DELIVERY_PROJECT


def repository_json_path(repo_root: Path) -> Path:
    return Path(repo_root) / "project" / "repository.json"


def load_repository_identity(repo_root: Path) -> RepositoryIdentity:
    """Load and structurally validate project/repository.json (fail closed)."""
    root = Path(repo_root).resolve()
    path = repository_json_path(root)
    if not path.is_file():
        raise RepositoryValidationError(
            f"repository.json missing at required path: {path}"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RepositoryValidationError(
            f"repository.json is malformed JSON at {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise RepositoryValidationError(
            f"repository.json must be a JSON object at {path}"
        )

    if "schema_version" not in raw:
        raise RepositoryValidationError(
            f"repository.json missing required field 'schema_version' at {path}"
        )
    try:
        schema_version = int(raw["schema_version"])
    except (TypeError, ValueError) as exc:
        raise RepositoryValidationError(
            f"repository.json schema_version must be an integer at {path}"
        ) from exc
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise RepositoryValidationError(
            f"repository.json unsupported schema_version {schema_version} at {path}; "
            f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )

    repository_type = raw.get("repository_type")
    if repository_type not in SUPPORTED_REPOSITORY_TYPES:
        raise RepositoryValidationError(
            f"repository.json repository_type must be one of "
            f"{sorted(SUPPORTED_REPOSITORY_TYPES)} (got {repository_type!r}) at {path}"
        )

    isolation_model = raw.get("isolation_model")
    if isolation_model != REQUIRED_ISOLATION_MODEL:
        raise RepositoryValidationError(
            f"repository.json isolation_model must be "
            f"{REQUIRED_ISOLATION_MODEL!r} (got {isolation_model!r}) at {path}"
        )

    project_human_id_raw = raw.get("project_human_id")
    project_name_raw = raw.get("project_name")

    if repository_type == REPOSITORY_TYPE_DELIVERY_TEMPLATE:
        if project_human_id_raw not in (None, "", "null"):
            raise RepositoryValidationError(
                f"delivery-template repository.json must have project_human_id "
                f"null (got {project_human_id_raw!r}) at {path}"
            )
        project_human_id = None
        if project_name_raw not in (None, "", "null"):
            raise RepositoryValidationError(
                f"delivery-template repository.json must have project_name "
                f"null (got {project_name_raw!r}) at {path}"
            )
        project_name = None
    else:
        if not project_human_id_raw or not str(project_human_id_raw).strip():
            raise RepositoryValidationError(
                f"delivery-project repository.json missing required "
                f"'project_human_id' at {path}"
            )
        project_human_id = str(project_human_id_raw).strip()
        project_name = (
            str(project_name_raw) if project_name_raw is not None else None
        )

    orchestration_scope = raw.get("orchestration_scope")
    cross = raw.get("cross_project_access")
    if cross is not None and not isinstance(cross, bool):
        raise RepositoryValidationError(
            f"repository.json cross_project_access must be boolean at {path}"
        )

    return RepositoryIdentity(
        schema_version=schema_version,
        repository_type=str(repository_type),
        project_human_id=project_human_id,
        isolation_model=str(isolation_model),
        project_name=project_name,
        orchestration_scope=str(orchestration_scope)
        if orchestration_scope is not None
        else None,
        cross_project_access=cross if isinstance(cross, bool) else None,
        path=path,
        raw=raw,
    )
