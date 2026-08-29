"""Path helpers for ProjectOS."""

from __future__ import annotations

from pathlib import Path

# src/projectos/paths.py -> ProjectOS repository root
PROJECTOS_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_PATH = PROJECTOS_ROOT / "config" / "projects.json"
SCHEMAS_DIR = PROJECTOS_ROOT / "schemas"
PROJECTS_SCHEMA_PATH = SCHEMAS_DIR / "projects.schema.json"
STATE_DIR = PROJECTOS_ROOT / "state"
DEFAULT_DB_PATH = STATE_DIR / "projectos.db"
MIGRATIONS_DIR = PROJECTOS_ROOT / "migrations"
LOGS_DIR = PROJECTOS_ROOT / "logs"
RUN_OUTPUT_DIR = LOGS_DIR / "runs"
DASHBOARD_DIST = PROJECTOS_ROOT / "web" / "dist"


def dashboard_index() -> Path:
    return DASHBOARD_DIST / "index.html"


def dashboard_is_built() -> bool:
    return dashboard_index().is_file()
