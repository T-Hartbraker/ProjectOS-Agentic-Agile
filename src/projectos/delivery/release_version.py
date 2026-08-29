"""Deterministic release version resolution from governed state."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from projectos.delivery.semver import parse_semver, propose_bump
from projectos.delivery.store import list_delivery_releases
from projectos.errors import OrchestrationError

_PYPROJECT_VERSION_RE = re.compile(
    r'^\s*version\s*=\s*["\']([^"\']+)["\']',
    re.MULTILINE,
)


def read_declared_version(repo_root: Path | None) -> str | None:
    if repo_root is None:
        return None
    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        match = _PYPROJECT_VERSION_RE.search(pyproject.read_text(encoding="utf-8"))
        if match:
            try:
                parse_semver(match.group(1))
                return match.group(1)
            except OrchestrationError:
                pass
    identity = repo_root / "project" / "repository.json"
    if identity.is_file():
        import json

        try:
            data = json.loads(identity.read_text(encoding="utf-8"))
            version = str(data.get("release_version") or "").strip()
            if version:
                parse_semver(version)
                return version
        except (json.JSONDecodeError, OrchestrationError):
            pass
    return None


def resolve_release_version(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    repo_root: Path | None,
    allow_republish: bool = False,
) -> str:
    """Resolve the next governed release version, avoiding accidental republication."""
    declared = read_declared_version(repo_root) or "0.1.0"
    published = {
        str(row["version"])
        for row in list_delivery_releases(conn, project_id)
        if str(row.get("publication_status") or "") == "published"
    }
    version = declared
    if version in published and not allow_republish:
        while version in published:
            version = propose_bump(version, change_type="patch")
    return version
