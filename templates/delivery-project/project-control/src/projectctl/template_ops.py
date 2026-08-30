"""Template initialization and preparation operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from projectctl.audit import write_audit
from projectctl.db import connect
from projectctl.ids import next_human_id
from projectctl.isolation import list_active_projects
from projectctl.migrate import initialize_database
from projectctl.paths import DEFAULT_DB_PATH
from projectctl.repository import (
    RepositoryIdentityError,
    delivery_project_manifest,
    find_repository_root,
    load_repository_identity,
    template_manifest,
    write_repository_identity,
)
from projectctl.store import StoreError, create_project


@dataclass(frozen=True)
class InitRepositoryResult:
    project: dict[str, Any]
    repository_path: Path
    identity: dict[str, Any]


@dataclass(frozen=True)
class PrepareTemplateResult:
    repo_root: Path
    database_path: Path
    repository_path: Path
    reported_project_specific_paths: list[str]
    actions: list[str]


# Paths that may contain project-specific content; never deleted silently.
_PROJECT_SPECIFIC_GLOBS = (
    "product",
    "project/PRJ-*",
    "project/intake",
    "releases",
)


def _resolve_root(repo_root: Path | str | None) -> Path:
    if repo_root is not None:
        return Path(repo_root).resolve()
    found = find_repository_root()
    if found is None:
        raise StoreError(
            "Could not locate repository root (project/repository.json missing)"
        )
    return found.resolve()


def _db_for_repo(repo_root: Path, db_path: Path | str | None) -> Path:
    if db_path is not None:
        return Path(db_path)
    # Prefer project-control/project.db under the target repo when present.
    candidate = repo_root / "project-control" / "project.db"
    if candidate.parent.is_dir():
        return candidate
    return DEFAULT_DB_PATH


def init_repository(
    name: str,
    *,
    description: str | None = None,
    repo_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> InitRepositoryResult:
    """Bind an unbound delivery-template to a new active delivery project."""
    if not name or not name.strip():
        raise StoreError("Project name is required for init-repository")

    root = _resolve_root(repo_root)
    try:
        ident = load_repository_identity(root)
    except RepositoryIdentityError as exc:
        raise StoreError(str(exc)) from exc

    if ident.is_bound_project:
        raise StoreError(
            "Initialization refused: repository is already bound as "
            f"delivery-project {ident.project_human_id} "
            f"({ident.project_name!r})."
        )
    if not ident.is_template:
        raise StoreError(
            f"Initialization refused: repository_type must be "
            f"'delivery-template' (got {ident.repository_type!r})"
        )

    path = _db_for_repo(root, db_path)
    if not path.exists():
        initialize_database(db_path=path)

    conn = connect(path)
    try:
        active = list_active_projects(conn)
        if active:
            ids = ", ".join(p["human_id"] for p in active)
            raise StoreError(
                "Initialization refused: active project(s) already exist "
                f"({ids}). A delivery-template must have zero active projects."
            )
        if len(active) > 1:  # pragma: no cover - defensive
            raise StoreError(
                "Initialization refused: multiple active projects present"
            )
        # Preview next id for audit context (create_project assigns for real).
        preview_id = next_human_id(conn, "project")
    finally:
        conn.close()

    # Create the sole active project without ordinary isolation create checks
    # (those reject templates by design). Binding happens immediately after.
    project = create_project(
        name.strip(),
        description=description,
        db_path=path,
        make_active=True,
        enforce_isolation=False,
        reason="project init-repository",
    )

    # Verify exactly one active after create
    conn = connect(path)
    try:
        active = list_active_projects(conn)
        if len(active) != 1 or active[0]["human_id"] != project["human_id"]:
            raise StoreError(
                "Initialization aborted: expected exactly one active project "
                f"after create; found {[p['human_id'] for p in active]}"
            )
        manifest = delivery_project_manifest(
            project_human_id=str(project["human_id"]),
            project_name=str(project["name"]),
            schema_version=ident.schema_version,
        )
        repo_path = write_repository_identity(root, manifest)
        write_audit(
            conn,
            action="repository.init-bind",
            entity_type="repository",
            entity_id=str(project["human_id"]),
            before_state=ident.raw,
            after_state=manifest,
            reason=(
                f"Bound delivery-template to {project['human_id']} "
                f"(previewed {preview_id})"
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return InitRepositoryResult(
        project=project,
        repository_path=repo_path,
        identity=manifest,
    )


def _report_project_specific_paths(repo_root: Path) -> list[str]:
    found: list[str] = []
    product = repo_root / "product"
    if product.exists():
        found.append(str(product.relative_to(repo_root)).replace("\\", "/"))
    releases = repo_root / "releases"
    if releases.exists() and any(releases.iterdir()):
        found.append("releases/")
    project_dir = repo_root / "project"
    if project_dir.is_dir():
        for child in sorted(project_dir.iterdir()):
            if child.name.startswith("PRJ-") or child.name == "intake":
                found.append(
                    str(child.relative_to(repo_root)).replace("\\", "/")
                )
    return found


def prepare_template(
    *,
    force: bool = False,
    repo_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> PrepareTemplateResult:
    """Convert a delivery-project repo into an unbound delivery-template.

    Destructive DB/identity reset requires --force. Does not delete Git history
    or silently remove product/project/release trees.
    """
    root = _resolve_root(repo_root)
    path = _db_for_repo(root, db_path)
    leftovers = _report_project_specific_paths(root)

    if not force:
        raise StoreError(
            "template prepare requires explicit --force. "
            "With --force this will: (1) replace local project-control database "
            f"at {path} with a clean migrated database, (2) rewrite "
            "project/repository.json as delivery-template (unbound), "
            "(3) preserve framework/governance/agents/skills/migrations/tests. "
            "It will NOT delete Git history. "
            "It will NOT silently delete project-specific trees; reported paths: "
            + (", ".join(leftovers) if leftovers else "(none detected)")
            + "."
        )

    try:
        before = load_repository_identity(root)
        before_raw = before.raw
    except RepositoryIdentityError:
        before_raw = None

    actions: list[str] = []

    # Reset database: remove sqlite files then re-migrate.
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix) if suffix else path
        if candidate.exists():
            candidate.unlink()
            actions.append(f"removed {candidate.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    initialize_database(db_path=path)
    actions.append(f"initialized clean database at {path}")

    # Verify zero active
    conn = connect(path)
    try:
        active = list_active_projects(conn)
        if active:
            raise StoreError(
                "template prepare failed: clean database unexpectedly has "
                f"active projects {[p['human_id'] for p in active]}"
            )
        manifest = template_manifest()
        repo_path = write_repository_identity(root, manifest)
        actions.append(f"wrote delivery-template identity at {repo_path}")
        write_audit(
            conn,
            action="repository.template-prepare",
            entity_type="repository",
            entity_id=None,
            before_state=before_raw,
            after_state=manifest,
            reason="template prepare --force",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if leftovers:
        actions.append(
            "left project-specific paths untouched (manual cleanup if needed): "
            + ", ".join(leftovers)
        )

    return PrepareTemplateResult(
        repo_root=root,
        database_path=path,
        repository_path=repo_path,
        reported_project_specific_paths=leftovers,
        actions=actions,
    )
