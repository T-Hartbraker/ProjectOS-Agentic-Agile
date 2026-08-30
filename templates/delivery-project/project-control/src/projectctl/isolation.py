"""Deterministic one-project-per-repository isolation checks."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from projectctl.db import connect
from projectctl.repository import (
    RepositoryIdentity,
    RepositoryIdentityError,
    load_repository_identity,
)


class ProjectIsolationError(Exception):
    """Active project state is inconsistent with repository identity."""


@dataclass(frozen=True)
class IsolationResult:
    identity: RepositoryIdentity
    active_project_human_id: str
    active_count: int


def list_active_projects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT human_id, name, status, is_active
        FROM projects
        WHERE is_active = 1
        ORDER BY id ASC
        """
    ).fetchall()
    return [{k: row[k] for k in row.keys()} for row in rows]


def validate_project_isolation(
    db_path: Path | str | None = None,
    *,
    repo_root: Path | str | None = None,
    identity: RepositoryIdentity | None = None,
) -> IsolationResult:
    """Fail closed unless exactly one active project matches repository.json.

    Never silently selects another project.
    Unbound delivery-template repositories cannot run project-scoped commands.
    """
    try:
        ident = identity or load_repository_identity(
            Path(repo_root) if repo_root is not None else None
        )
    except RepositoryIdentityError:
        raise

    if ident.is_template:
        raise ProjectIsolationError(
            "Repository is an unbound delivery-template (no project_human_id). "
            "Normal project-scoped commands are blocked until initialization. "
            'Run: python -m projectctl project init-repository --name "<Project Name>"'
        )

    if not ident.project_human_id:
        raise ProjectIsolationError(
            "Project isolation violation: delivery-project identity is missing "
            "project_human_id."
        )

    conn = connect(db_path)
    try:
        active = list_active_projects(conn)
    finally:
        conn.close()

    if len(active) == 0:
        raise ProjectIsolationError(
            "Project isolation violation: no active project in project-control. "
            f"repository.json requires active project {ident.project_human_id}."
        )
    if len(active) > 1:
        ids = ", ".join(p["human_id"] for p in active)
        raise ProjectIsolationError(
            "Project isolation violation: multiple active projects "
            f"({ids}). Exactly one active delivery project is required; "
            "refusing to select among them."
        )

    active_id = str(active[0]["human_id"])
    if active_id != ident.project_human_id:
        raise ProjectIsolationError(
            "Project isolation violation: active project "
            f"{active_id} does not match repository identity "
            f"{ident.project_human_id} "
            f"(from {ident.path}). Refusing to continue."
        )

    return IsolationResult(
        identity=ident,
        active_project_human_id=active_id,
        active_count=1,
    )


def assert_can_activate_project(
    human_id: str,
    *,
    repo_root: Path | str | None = None,
    identity: RepositoryIdentity | None = None,
) -> RepositoryIdentity:
    """Reject activating a project that is not the repository identity."""
    ident = identity or load_repository_identity(
        Path(repo_root) if repo_root is not None else None
    )
    if ident.is_template:
        raise ProjectIsolationError(
            "Cannot activate a project in an unbound delivery-template. "
            'Use: python -m projectctl project init-repository --name "..."'
        )
    if human_id != ident.project_human_id:
        raise ProjectIsolationError(
            "Project isolation violation: cannot activate "
            f"{human_id} in this repository. "
            f"repository.json binds this repository to {ident.project_human_id}."
        )
    return ident


def assert_can_create_active_project(
    conn: sqlite3.Connection,
    *,
    next_human_id: str,
    repo_root: Path | str | None = None,
    identity: RepositoryIdentity | None = None,
) -> RepositoryIdentity:
    """Reject creating a second / mismatched active delivery project."""
    ident = identity or load_repository_identity(
        Path(repo_root) if repo_root is not None else None
    )
    if ident.is_template:
        raise ProjectIsolationError(
            "Cannot create an active project via ordinary create in a "
            "delivery-template. "
            'Use: python -m projectctl project init-repository --name "..."'
        )
    active = list_active_projects(conn)
    if active:
        ids = ", ".join(p["human_id"] for p in active)
        raise ProjectIsolationError(
            "Project isolation violation: cannot create another active project; "
            f"already active: {ids}. "
            f"This repository is bound to {ident.project_human_id}."
        )
    if next_human_id != ident.project_human_id:
        raise ProjectIsolationError(
            "Project isolation violation: next project id "
            f"{next_human_id} would not match repository identity "
            f"{ident.project_human_id}. Create inactive historical records only, "
            "or repair identity with an administrative command."
        )
    return ident
