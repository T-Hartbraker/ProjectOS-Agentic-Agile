"""Path helpers for project-control."""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PACKAGE_ROOT / "project.db"
MIGRATIONS_DIR = PACKAGE_ROOT / "migrations"
