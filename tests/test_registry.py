"""Unit tests for ProjectOS registry loading and schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from projectos.errors import RegistryConflictError, RegistryError
from projectos.registry import load_registry
from helpers import schema_path, write_registry


def test_load_valid_registry(tmp_path: Path) -> None:
    root = str((tmp_path / "repo").resolve())
    path = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-003",
                "repository_root": root,
                "enabled": True,
            }
        ],
    )
    registry = load_registry(path, schema_path=schema_path())
    assert registry.schema_version == 1
    assert len(registry.projects) == 1
    assert registry.projects[0].project_human_id == "PRJ-003"
    assert registry.projects[0].enabled is True


def test_malformed_registry_json(tmp_path: Path) -> None:
    path = write_registry(tmp_path / "projects.json", [], corrupt=True)
    with pytest.raises(RegistryError, match="malformed JSON"):
        load_registry(path, schema_path=schema_path())


def test_missing_registry_file(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="missing"):
        load_registry(tmp_path / "nope.json", schema_path=schema_path())


def test_relative_repository_root_rejected(tmp_path: Path) -> None:
    path = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-001",
                "repository_root": "relative/path",
                "enabled": True,
            }
        ],
    )
    with pytest.raises(RegistryError, match="absolute path"):
        load_registry(path, schema_path=schema_path())


def test_duplicate_registered_ids(tmp_path: Path) -> None:
    root_a = str((tmp_path / "a").resolve())
    root_b = str((tmp_path / "b").resolve())
    path = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-001",
                "repository_root": root_a,
                "enabled": True,
            },
            {
                "project_human_id": "PRJ-001",
                "repository_root": root_b,
                "enabled": False,
            },
        ],
    )
    with pytest.raises(RegistryConflictError, match="Duplicate registered project_human_id"):
        load_registry(path, schema_path=schema_path())


def test_duplicate_registered_roots(tmp_path: Path) -> None:
    root = str((tmp_path / "same").resolve())
    path = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-001",
                "repository_root": root,
                "enabled": True,
            },
            {
                "project_human_id": "PRJ-002",
                "repository_root": root,
                "enabled": True,
            },
        ],
    )
    with pytest.raises(RegistryConflictError, match="Duplicate registered repository_root"):
        load_registry(path, schema_path=schema_path())


def test_missing_required_entry_fields(tmp_path: Path) -> None:
    path = write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-001", "enabled": True}],
    )
    with pytest.raises(RegistryError, match="missing required fields"):
        load_registry(path, schema_path=schema_path())


def test_enabled_must_be_boolean(tmp_path: Path) -> None:
    path = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-001",
                "repository_root": str((tmp_path / "repo").resolve()),
                "enabled": "yes",
            }
        ],
    )
    with pytest.raises(RegistryError, match="enabled must be a boolean"):
        load_registry(path, schema_path=schema_path())
