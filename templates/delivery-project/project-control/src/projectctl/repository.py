"""Load and validate project/repository.json identity."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RepositoryIdentityError(Exception):
    """repository.json missing, malformed, or unsupported."""


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


def find_repository_root(start: Path | None = None) -> Path | None:
    """Walk parents from start (default cwd) looking for project/repository.json."""
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "project" / "repository.json").is_file():
            return candidate
    return None


def repository_json_path(repo_root: Path) -> Path:
    return Path(repo_root) / "project" / "repository.json"


def template_manifest() -> dict[str, Any]:
    """Canonical unbound delivery-template identity document."""
    return {
        "schema_version": 1,
        "repository_type": REPOSITORY_TYPE_DELIVERY_TEMPLATE,
        "project_human_id": None,
        "project_name": None,
        "isolation_model": REQUIRED_ISOLATION_MODEL,
        "orchestration_scope": "project",
        "cross_project_access": False,
    }


def delivery_project_manifest(
    *,
    project_human_id: str,
    project_name: str,
    schema_version: int = 1,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "repository_type": REPOSITORY_TYPE_DELIVERY_PROJECT,
        "project_human_id": project_human_id,
        "project_name": project_name,
        "isolation_model": REQUIRED_ISOLATION_MODEL,
        "orchestration_scope": "project",
        "cross_project_access": False,
    }


def write_repository_identity(repo_root: Path, document: dict[str, Any]) -> Path:
    """Atomically write project/repository.json (UTF-8, no BOM)."""
    path = repository_json_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2, sort_keys=False) + "\n"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path


def load_repository_identity(repo_root: Path | None = None) -> RepositoryIdentity:
    """Load and structurally validate project/repository.json (fail closed)."""
    root = Path(repo_root) if repo_root is not None else find_repository_root()
    if root is None:
        raise RepositoryIdentityError(
            "repository.json not found: could not locate project/repository.json "
            "from the current directory. A delivery repository must declare its "
            "project identity."
        )
    path = repository_json_path(root)
    if not path.is_file():
        raise RepositoryIdentityError(
            f"repository.json missing at required path: {path}"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RepositoryIdentityError(
            f"repository.json is malformed JSON at {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise RepositoryIdentityError(
            f"repository.json must be a JSON object at {path}"
        )

    if "schema_version" not in raw:
        raise RepositoryIdentityError(
            f"repository.json missing required field 'schema_version' at {path}"
        )
    try:
        schema_version = int(raw["schema_version"])
    except (TypeError, ValueError) as exc:
        raise RepositoryIdentityError(
            f"repository.json schema_version must be an integer at {path}"
        ) from exc
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise RepositoryIdentityError(
            f"repository.json unsupported schema_version {schema_version} at {path}; "
            f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )

    repository_type = raw.get("repository_type")
    if repository_type not in SUPPORTED_REPOSITORY_TYPES:
        raise RepositoryIdentityError(
            f"repository.json repository_type must be one of "
            f"{sorted(SUPPORTED_REPOSITORY_TYPES)} (got {repository_type!r}) at {path}"
        )

    isolation_model = raw.get("isolation_model")
    if isolation_model != REQUIRED_ISOLATION_MODEL:
        raise RepositoryIdentityError(
            f"repository.json isolation_model must be "
            f"{REQUIRED_ISOLATION_MODEL!r} (got {isolation_model!r}) at {path}"
        )

    project_human_id_raw = raw.get("project_human_id")
    project_name_raw = raw.get("project_name")

    if repository_type == REPOSITORY_TYPE_DELIVERY_TEMPLATE:
        if project_human_id_raw not in (None, "", "null"):
            raise RepositoryIdentityError(
                f"delivery-template repository.json must have project_human_id "
                f"null (got {project_human_id_raw!r}) at {path}"
            )
        project_human_id = None
        if project_name_raw not in (None, "", "null"):
            raise RepositoryIdentityError(
                f"delivery-template repository.json must have project_name "
                f"null (got {project_name_raw!r}) at {path}"
            )
        project_name = None
    else:
        if not project_human_id_raw or not str(project_human_id_raw).strip():
            raise RepositoryIdentityError(
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
        raise RepositoryIdentityError(
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
