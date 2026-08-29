"""Load and validate config/projects.json registry."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from projectos.errors import RegistryConflictError, RegistryError
from projectos.paths import DEFAULT_REGISTRY_PATH, PROJECTS_SCHEMA_PATH

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})


@dataclass(frozen=True)
class RegistryEntry:
    project_human_id: str
    repository_root: Path
    enabled: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class ProjectRegistry:
    schema_version: int
    projects: tuple[RegistryEntry, ...]
    path: Path

    def enabled_projects(self) -> tuple[RegistryEntry, ...]:
        return tuple(p for p in self.projects if p.enabled)

    def get(self, project_human_id: str) -> RegistryEntry | None:
        for entry in self.projects:
            if entry.project_human_id == project_human_id:
                return entry
        return None


def _is_absolute_path_string(value: str) -> bool:
    """Accept POSIX or Windows absolute paths regardless of host OS."""
    if not value or not str(value).strip():
        return False
    text = str(value).strip()
    return PureWindowsPath(text).is_absolute() or PurePosixPath(text).is_absolute()


def _validate_against_schema(document: dict[str, Any], schema_path: Path) -> None:
    """Structural validation aligned with schemas/projects.schema.json.

    Implemented without an external JSON Schema library so ProjectOS stays
    dependency-light; the on-disk schema remains the contract document.
    """
    if not schema_path.is_file():
        raise RegistryError(f"Registry schema missing at {schema_path}")

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RegistryError(
            f"Registry schema is malformed JSON at {schema_path}: {exc}"
        ) from exc
    if not isinstance(schema, dict):
        raise RegistryError(f"Registry schema must be a JSON object at {schema_path}")

    if "schema_version" not in document:
        raise RegistryError("projects.json missing required field 'schema_version'")
    try:
        schema_version = int(document["schema_version"])
    except (TypeError, ValueError) as exc:
        raise RegistryError(
            "projects.json schema_version must be an integer"
        ) from exc
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise RegistryError(
            f"projects.json unsupported schema_version {schema_version}; "
            f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )

    if "projects" not in document:
        raise RegistryError("projects.json missing required field 'projects'")
    projects = document["projects"]
    if not isinstance(projects, list):
        raise RegistryError("projects.json 'projects' must be an array")

    allowed_keys = {"schema_version", "projects"}
    extra = set(document) - allowed_keys
    if extra:
        raise RegistryError(
            f"projects.json contains unsupported top-level fields: {sorted(extra)}"
        )

    entry_required = {"project_human_id", "repository_root", "enabled"}
    entry_allowed = set(entry_required)
    for index, item in enumerate(projects):
        prefix = f"projects[{index}]"
        if not isinstance(item, dict):
            raise RegistryError(f"{prefix} must be a JSON object")
        missing = entry_required - set(item)
        if missing:
            raise RegistryError(
                f"{prefix} missing required fields: {sorted(missing)}"
            )
        extra_entry = set(item) - entry_allowed
        if extra_entry:
            raise RegistryError(
                f"{prefix} contains unsupported fields: {sorted(extra_entry)}"
            )

        human_id = item["project_human_id"]
        if not isinstance(human_id, str) or not human_id.strip():
            raise RegistryError(
                f"{prefix}.project_human_id must be a non-empty string"
            )

        root = item["repository_root"]
        if not isinstance(root, str) or not root.strip():
            raise RegistryError(
                f"{prefix}.repository_root must be a non-empty string"
            )
        if not _is_absolute_path_string(root):
            raise RegistryError(
                f"{prefix}.repository_root must be an absolute path "
                f"(got {root!r})"
            )

        enabled = item["enabled"]
        if not isinstance(enabled, bool):
            raise RegistryError(f"{prefix}.enabled must be a boolean")


def _detect_duplicates(entries: list[RegistryEntry]) -> None:
    seen_ids: dict[str, int] = {}
    seen_roots: dict[str, int] = {}
    for index, entry in enumerate(entries):
        human_id = entry.project_human_id
        if human_id in seen_ids:
            raise RegistryConflictError(
                f"Duplicate registered project_human_id {human_id!r} "
                f"at projects[{seen_ids[human_id]}] and projects[{index}]"
            )
        seen_ids[human_id] = index

        root_key = str(entry.repository_root).casefold()
        if root_key in seen_roots:
            raise RegistryConflictError(
                f"Duplicate registered repository_root {entry.repository_root} "
                f"at projects[{seen_roots[root_key]}] and projects[{index}]"
            )
        seen_roots[root_key] = index


def load_registry(
    path: Path | str | None = None,
    *,
    schema_path: Path | str | None = None,
) -> ProjectRegistry:
    """Load config/projects.json with schema validation (fail closed)."""
    registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    schema = Path(schema_path) if schema_path is not None else PROJECTS_SCHEMA_PATH

    if not registry_path.is_file():
        raise RegistryError(f"Project registry missing at {registry_path}")

    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RegistryError(
            f"projects.json is malformed JSON at {registry_path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise RegistryError(
            f"projects.json must be a JSON object at {registry_path}"
        )

    _validate_against_schema(raw, schema)

    entries: list[RegistryEntry] = []
    for item in raw["projects"]:
        entries.append(
            RegistryEntry(
                project_human_id=str(item["project_human_id"]).strip(),
                repository_root=Path(str(item["repository_root"]).strip()),
                enabled=bool(item["enabled"]),
                raw=dict(item),
            )
        )

    _detect_duplicates(entries)

    return ProjectRegistry(
        schema_version=int(raw["schema_version"]),
        projects=tuple(entries),
        path=registry_path.resolve(),
    )


def empty_registry(path: Path | str) -> ProjectRegistry:
    """In-memory empty registry used for first-time governed onboarding."""
    return ProjectRegistry(
        schema_version=1,
        projects=(),
        path=Path(path).resolve(),
    )


def load_registry_or_empty(
    path: Path | str | None = None,
    *,
    schema_path: Path | str | None = None,
) -> ProjectRegistry:
    registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    if not registry_path.is_file():
        return empty_registry(registry_path)
    return load_registry(registry_path, schema_path=schema_path)


def registry_document(registry: ProjectRegistry) -> dict[str, Any]:
    return {
        "schema_version": int(registry.schema_version),
        "projects": [
            {
                "project_human_id": entry.project_human_id,
                "repository_root": str(Path(entry.repository_root).resolve()),
                "enabled": bool(entry.enabled),
            }
            for entry in registry.projects
        ],
    }


def persist_registry(
    registry: ProjectRegistry,
    *,
    schema_path: Path | str | None = None,
) -> ProjectRegistry:
    """Validate the full document, then replace projects.json atomically.

    The live registry file is not truncated or rewritten in place. A failed
    persist leaves the previous file (or absence of file) unchanged.
    """
    path = Path(registry.path)
    schema = Path(schema_path) if schema_path is not None else PROJECTS_SCHEMA_PATH
    document = registry_document(registry)
    _validate_against_schema(document, schema)
    rebuilt = [
        RegistryEntry(
            project_human_id=str(item["project_human_id"]).strip(),
            repository_root=Path(str(item["repository_root"]).strip()),
            enabled=bool(item["enabled"]),
            raw=dict(item),
        )
        for item in document["projects"]
    ]
    _detect_duplicates(rebuilt)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".projects-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        replaced = True
    finally:
        if not replaced and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    return load_registry(path, schema_path=schema)


def replace_entry(registry: ProjectRegistry, entry: RegistryEntry) -> ProjectRegistry:
    projects = tuple(
        entry if existing.project_human_id == entry.project_human_id else existing
        for existing in registry.projects
    )
    if not any(p.project_human_id == entry.project_human_id for p in registry.projects):
        projects = registry.projects + (entry,)
    return replace(registry, projects=projects)
