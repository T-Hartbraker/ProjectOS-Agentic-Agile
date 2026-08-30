"""Shared pytest fixtures for projectctl tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from projectctl.migrate import initialize_database


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test-project.db"
    initialize_database(db_path=path)
    return path
