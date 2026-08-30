"""Domain operations for project-control entities."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from projectctl.audit import row_to_dict, write_audit
from projectctl.db import connect
from projectctl.ids import next_human_id
from projectctl.migrate import initialize_database
from projectctl.paths import DEFAULT_DB_PATH

ALLOWED_CUSTOM_TYPES = frozenset(
    {"text", "integer", "real", "boolean", "date", "datetime", "json"}
)


class StoreError(Exception):
    """Domain/business rule error."""


def _get_project_by_human_id(conn: sqlite3.Connection, human_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM projects WHERE human_id = ?", (human_id,)
    ).fetchone()
    if row is None:
        raise StoreError(f"Project not found: {human_id}")
    return row


def _get_active_project(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM projects WHERE is_active = 1 ORDER BY id LIMIT 1"
    ).fetchone()


def _require_active_project(conn: sqlite3.Connection) -> sqlite3.Row:
    row = _get_active_project(conn)
    if row is None:
        raise StoreError("No active project. Create a project first.")
    return row


def _resolve_project(
    conn: sqlite3.Connection, project_id: str | None
) -> sqlite3.Row:
    if project_id:
        return _get_project_by_human_id(conn, project_id)
    return _require_active_project(conn)


# ---------------------------------------------------------------------------
# Init / status / audit
# ---------------------------------------------------------------------------


def init_database(db_path: Path | str | None = None) -> list[str]:
    return initialize_database(db_path=db_path)


def get_status(db_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    if not path.exists():
        return {
            "initialized": False,
            "message": "No project database found. Run: python -m projectctl init",
            "active_project": None,
            "counts": {},
        }

    conn = connect(path)
    try:
        active = _get_active_project(conn)
        if active is None:
            project_count = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
            return {
                "initialized": True,
                "message": "No active project."
                + (f" ({project_count} project(s) exist; none marked active.)" if project_count else ""),
                "active_project": None,
                "counts": {},
            }

        pid = active["id"]
        counts = {
            "requirements": conn.execute(
                "SELECT COUNT(*) AS c FROM requirements WHERE project_id = ?", (pid,)
            ).fetchone()["c"],
            "stories": conn.execute(
                "SELECT COUNT(*) AS c FROM stories WHERE project_id = ?", (pid,)
            ).fetchone()["c"],
            "defects": conn.execute(
                "SELECT COUNT(*) AS c FROM defects WHERE project_id = ?", (pid,)
            ).fetchone()["c"],
            "risks": conn.execute(
                "SELECT COUNT(*) AS c FROM risks WHERE project_id = ?", (pid,)
            ).fetchone()["c"],
            "assumptions": conn.execute(
                "SELECT COUNT(*) AS c FROM assumptions WHERE project_id = ?", (pid,)
            ).fetchone()["c"],
            "decisions": conn.execute(
                "SELECT COUNT(*) AS c FROM decisions WHERE project_id = ?", (pid,)
            ).fetchone()["c"],
            "iterations": conn.execute(
                "SELECT COUNT(*) AS c FROM iterations WHERE project_id = ?", (pid,)
            ).fetchone()["c"],
            "releases": conn.execute(
                "SELECT COUNT(*) AS c FROM releases WHERE project_id = ?", (pid,)
            ).fetchone()["c"],
            "open_defects": conn.execute(
                "SELECT COUNT(*) AS c FROM defects WHERE project_id = ? AND status = 'open'",
                (pid,),
            ).fetchone()["c"],
            "trace_links": conn.execute(
                "SELECT COUNT(*) AS c FROM trace_links WHERE project_id = ?", (pid,)
            ).fetchone()["c"],
        }
        return {
            "initialized": True,
            "message": f"Active project: {active['human_id']} - {active['name']}",
            "active_project": row_to_dict(active),
            "counts": counts,
        }
    finally:
        conn.close()


def list_audit(
    db_path: Path | str | None = None,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM audit_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def create_project(
    name: str,
    *,
    description: str | None = None,
    db_path: Path | str | None = None,
    make_active: bool = True,
    actor_type: str = "cli",
    actor_id: str | None = None,
    reason: str | None = None,
    enforce_isolation: bool = False,
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    if not name or not name.strip():
        raise StoreError("Project name is required")

    conn = connect(db_path)
    try:
        human_id = next_human_id(conn, "project")
        if enforce_isolation and make_active:
            from projectctl.isolation import (
                ProjectIsolationError,
                assert_can_create_active_project,
            )
            from projectctl.repository import RepositoryIdentityError

            try:
                assert_can_create_active_project(
                    conn,
                    next_human_id=human_id,
                    repo_root=repo_root,
                )
            except (ProjectIsolationError, RepositoryIdentityError) as exc:
                raise StoreError(str(exc)) from exc
        elif enforce_isolation and not make_active:
            # Inactive creates are allowed (historical/smoke) but identity must load.
            from projectctl.repository import (
                RepositoryIdentityError,
                load_repository_identity,
            )

            try:
                load_repository_identity(
                    Path(repo_root) if repo_root is not None else None
                )
            except RepositoryIdentityError as exc:
                raise StoreError(str(exc)) from exc

        if make_active:
            conn.execute("UPDATE projects SET is_active = 0 WHERE is_active = 1")
        cur = conn.execute(
            """
            INSERT INTO projects (human_id, name, description, status, is_active)
            VALUES (?, ?, ?, 'active', ?)
            """,
            (human_id, name.strip(), description, 1 if make_active else 0),
        )
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        after = row_to_dict(row)
        write_audit(
            conn,
            action="create",
            entity_type="project",
            entity_id=human_id,
            before_state=None,
            after_state=after,
            reason=reason or "project create",
            actor_type=actor_type,
            actor_id=actor_id,
        )
        conn.commit()
        return after  # type: ignore[return-value]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def activate_project(
    human_id: str,
    *,
    db_path: Path | str | None = None,
    enforce_isolation: bool = True,
    repo_root: Path | str | None = None,
    actor_type: str = "cli",
    actor_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Activate exactly one project; isolation prevents unrelated activation."""
    conn = connect(db_path)
    try:
        row = _get_project_by_human_id(conn, human_id)
        before = row_to_dict(row)
        if enforce_isolation:
            from projectctl.isolation import (
                ProjectIsolationError,
                assert_can_activate_project,
            )
            from projectctl.repository import RepositoryIdentityError

            try:
                assert_can_activate_project(human_id, repo_root=repo_root)
            except (ProjectIsolationError, RepositoryIdentityError) as exc:
                raise StoreError(str(exc)) from exc

        conn.execute("UPDATE projects SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            """
            UPDATE projects
            SET is_active = 1, updated_at = datetime('now')
            WHERE human_id = ?
            """,
            (human_id,),
        )
        after_row = conn.execute(
            "SELECT * FROM projects WHERE human_id = ?", (human_id,)
        ).fetchone()
        after = row_to_dict(after_row)
        write_audit(
            conn,
            action="activate",
            entity_type="project",
            entity_id=human_id,
            before_state=before,
            after_state=after,
            reason=reason or "project activate",
            actor_type=actor_type,
            actor_id=actor_id,
        )
        conn.commit()
        return after  # type: ignore[return-value]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_projects(db_path: Path | str | None = None) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY id ASC"
        ).fetchall()
        return [row_to_dict(r) for r in rows]  # type: ignore[misc]
    finally:
        conn.close()


def show_project(
    human_id: str,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        row = _get_project_by_human_id(conn, human_id)
        return row_to_dict(row)  # type: ignore[return-value]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Generic project-scoped entity helpers
# ---------------------------------------------------------------------------


def _create_named_entity(
    conn: sqlite3.Connection,
    *,
    entity_key: str,
    table: str,
    project_row: sqlite3.Row,
    columns: dict[str, Any],
    reason: str,
    actor_type: str = "cli",
    actor_id: str | None = None,
) -> dict[str, Any]:
    human_id = next_human_id(conn, entity_key)
    col_names = ["human_id", "project_id", *columns.keys()]
    values = [human_id, project_row["id"], *columns.values()]
    placeholders = ", ".join("?" for _ in col_names)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(col_names)}) VALUES ({placeholders})",
        values,
    )
    row = conn.execute(
        f"SELECT * FROM {table} WHERE human_id = ?", (human_id,)
    ).fetchone()
    after = row_to_dict(row)
    write_audit(
        conn,
        action="create",
        entity_type=entity_key,
        entity_id=human_id,
        after_state=after,
        reason=reason,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    return after  # type: ignore[return-value]


def _list_for_project(
    conn: sqlite3.Connection, table: str, project_row: sqlite3.Row
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE project_id = ? ORDER BY id ASC",
        (project_row["id"],),
    ).fetchall()
    return [row_to_dict(r) for r in rows]  # type: ignore[misc]


def _show_by_human_id(
    conn: sqlite3.Connection, table: str, human_id: str, label: str
) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT * FROM {table} WHERE human_id = ?", (human_id,)
    ).fetchone()
    if row is None:
        raise StoreError(f"{label} not found: {human_id}")
    return row_to_dict(row)  # type: ignore[return-value]


def create_requirement(
    title: str,
    *,
    description: str | None = None,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        result = _create_named_entity(
            conn,
            entity_key="requirement",
            table="requirements",
            project_row=project,
            columns={"title": title, "description": description, "status": "draft"},
            reason="requirement create",
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_requirements(
    *,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        return _list_for_project(conn, "requirements", project)
    finally:
        conn.close()


def show_requirement(
    human_id: str, db_path: Path | str | None = None
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        return _show_by_human_id(conn, "requirements", human_id, "Requirement")
    finally:
        conn.close()


def create_story(
    title: str,
    *,
    description: str | None = None,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        result = _create_named_entity(
            conn,
            entity_key="story",
            table="stories",
            project_row=project,
            columns={"title": title, "description": description, "status": "backlog"},
            reason="story create",
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_stories(
    *,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        return _list_for_project(conn, "stories", project)
    finally:
        conn.close()


def show_story(human_id: str, db_path: Path | str | None = None) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        return _show_by_human_id(conn, "stories", human_id, "Story")
    finally:
        conn.close()


def create_defect(
    title: str,
    *,
    description: str | None = None,
    severity: str = "medium",
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        result = _create_named_entity(
            conn,
            entity_key="defect",
            table="defects",
            project_row=project,
            columns={
                "title": title,
                "description": description,
                "severity": severity,
                "status": "open",
            },
            reason="defect create",
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_defects(
    *,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        return _list_for_project(conn, "defects", project)
    finally:
        conn.close()


def show_defect(human_id: str, db_path: Path | str | None = None) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        return _show_by_human_id(conn, "defects", human_id, "Defect")
    finally:
        conn.close()


def create_risk(
    title: str,
    *,
    description: str | None = None,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        result = _create_named_entity(
            conn,
            entity_key="risk",
            table="risks",
            project_row=project,
            columns={"title": title, "description": description, "status": "open"},
            reason="risk create",
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_risks(
    *,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        return _list_for_project(conn, "risks", project)
    finally:
        conn.close()


def show_risk(human_id: str, db_path: Path | str | None = None) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        return _show_by_human_id(conn, "risks", human_id, "Risk")
    finally:
        conn.close()


def create_assumption(
    statement: str,
    *,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        result = _create_named_entity(
            conn,
            entity_key="assumption",
            table="assumptions",
            project_row=project,
            columns={"statement": statement, "status": "open"},
            reason="assumption create",
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_assumptions(
    *,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        return _list_for_project(conn, "assumptions", project)
    finally:
        conn.close()


def show_assumption(
    human_id: str, db_path: Path | str | None = None
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        return _show_by_human_id(conn, "assumptions", human_id, "Assumption")
    finally:
        conn.close()


def create_decision(
    title: str,
    decision: str,
    *,
    rationale: str | None = None,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        result = _create_named_entity(
            conn,
            entity_key="decision",
            table="decisions",
            project_row=project,
            columns={
                "title": title,
                "decision": decision,
                "rationale": rationale,
                "status": "accepted",
            },
            reason="decision create",
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_decisions(
    *,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        return _list_for_project(conn, "decisions", project)
    finally:
        conn.close()


def show_decision(
    human_id: str, db_path: Path | str | None = None
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        return _show_by_human_id(conn, "decisions", human_id, "Decision")
    finally:
        conn.close()


def create_iteration(
    name: str,
    *,
    goal: str | None = None,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        result = _create_named_entity(
            conn,
            entity_key="iteration",
            table="iterations",
            project_row=project,
            columns={"name": name, "goal": goal, "status": "planned"},
            reason="iteration create",
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_iterations(
    *,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        return _list_for_project(conn, "iterations", project)
    finally:
        conn.close()


def show_iteration(
    human_id: str, db_path: Path | str | None = None
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        return _show_by_human_id(conn, "iterations", human_id, "Iteration")
    finally:
        conn.close()


def create_release(
    name: str,
    *,
    version: str | None = None,
    project_id: str | None = None,
    iteration_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        columns: dict[str, Any] = {
            "name": name,
            "version": version,
            "status": "planned",
        }
        if iteration_id:
            from projectctl.release_lifecycle import (
                ReleaseLifecycleError,
                _resolve_iteration_pk,
            )

            try:
                columns["iteration_id"] = _resolve_iteration_pk(
                    conn, int(project["id"]), iteration_id
                )
            except ReleaseLifecycleError as exc:
                raise StoreError(str(exc)) from exc
        result = _create_named_entity(
            conn,
            entity_key="release",
            table="releases",
            project_row=project,
            columns=columns,
            reason="release create",
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_releases(
    *,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        rows = conn.execute(
            """
            SELECT r.human_id, r.name, r.version, r.status, r.git_sha,
                   r.released_at, i.human_id AS iteration
            FROM releases r
            LEFT JOIN iterations i ON i.id = r.iteration_id
            WHERE r.project_id = ?
            ORDER BY r.id
            """,
            (project["id"],),
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


def show_release(
    human_id: str, db_path: Path | str | None = None
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT r.*,
                   p.human_id AS project_human_id,
                   i.human_id AS iteration
            FROM releases r
            JOIN projects p ON p.id = r.project_id
            LEFT JOIN iterations i ON i.id = r.iteration_id
            WHERE r.human_id = ?
            """,
            (human_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"Release not found: {human_id}")
        return row_to_dict(row)  # type: ignore[return-value]
    finally:
        conn.close()


def transition_release_status(
    human_id: str,
    target_status: str,
    *,
    git_sha: str | None = None,
    dirty_tree_exception: str | None = None,
    artifact_ref: str | None = None,
    qa_evidence_ref: str | None = None,
    iteration_id: str | None = None,
    reason: str | None = None,
    git_snapshot: Any = None,
    repo_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    from projectctl.release_lifecycle import (
        ReleaseLifecycleError,
        apply_release_transition,
    )

    conn = connect(db_path)
    try:
        result = apply_release_transition(
            conn,
            human_id,
            target_status,
            git_sha=git_sha,
            dirty_tree_exception=dirty_tree_exception,
            artifact_ref=artifact_ref,
            qa_evidence_ref=qa_evidence_ref,
            iteration_id=iteration_id,
            reason=reason,
            git_snapshot=git_snapshot,
            repo_root=Path(repo_root) if repo_root else None,
        )
        conn.commit()
        return result
    except ReleaseLifecycleError as exc:
        conn.rollback()
        raise StoreError(str(exc)) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_release(
    human_id: str,
    *,
    artifact_ref: str | None = None,
    reason: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    from projectctl.release_lifecycle import (
        ReleaseLifecycleError,
        complete_release as _complete,
    )

    conn = connect(db_path)
    try:
        result = _complete(
            conn,
            human_id,
            artifact_ref=artifact_ref,
            reason=reason,
        )
        conn.commit()
        return result
    except ReleaseLifecycleError as exc:
        conn.rollback()
        raise StoreError(str(exc)) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Custom fields
# ---------------------------------------------------------------------------


def create_custom_field_definition(
    *,
    entity_type: str,
    field_key: str,
    display_name: str,
    data_type: str,
    description: str | None = None,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    if data_type not in ALLOWED_CUSTOM_TYPES:
        raise StoreError(
            f"Unsupported data_type {data_type!r}; "
            f"allowed: {', '.join(sorted(ALLOWED_CUSTOM_TYPES))}"
        )
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        cur = conn.execute(
            """
            INSERT INTO custom_field_definitions (
                project_id, entity_type, field_key, display_name, data_type, description
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                project["id"],
                entity_type,
                field_key,
                display_name,
                data_type,
                description,
            ),
        )
        row = conn.execute(
            "SELECT * FROM custom_field_definitions WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        after = row_to_dict(row)
        write_audit(
            conn,
            action="create",
            entity_type="custom_field_definition",
            entity_id=str(after["id"]),
            after_state=after,
            reason="custom field definition create",
        )
        conn.commit()
        return after  # type: ignore[return-value]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_custom_field_value(
    *,
    definition_id: int,
    entity_id: str,
    value: Any,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        definition = conn.execute(
            "SELECT * FROM custom_field_definitions WHERE id = ?",
            (definition_id,),
        ).fetchone()
        if definition is None:
            raise StoreError(f"Custom field definition not found: {definition_id}")

        value_text = None
        value_integer = None
        value_real = None
        value_boolean = None
        data_type = definition["data_type"]

        if data_type == "text":
            value_text = str(value)
        elif data_type == "integer":
            value_integer = int(value)
            value_text = str(value_integer)
        elif data_type == "real":
            value_real = float(value)
            value_text = str(value_real)
        elif data_type == "boolean":
            if isinstance(value, bool):
                value_boolean = 1 if value else 0
            elif str(value).lower() in {"1", "true", "yes", "on"}:
                value_boolean = 1
            elif str(value).lower() in {"0", "false", "no", "off"}:
                value_boolean = 0
            else:
                raise StoreError(f"Invalid boolean value: {value!r}")
            value_text = "true" if value_boolean else "false"
        elif data_type in {"date", "datetime"}:
            value_text = str(value)
        elif data_type == "json":
            if isinstance(value, str):
                json.loads(value)  # validate
                value_text = value
            else:
                value_text = json.dumps(value)
        else:
            raise StoreError(f"Unsupported data_type: {data_type}")

        conn.execute(
            """
            INSERT INTO custom_field_values (
                definition_id, entity_id, value_text, value_integer, value_real, value_boolean
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(definition_id, entity_id) DO UPDATE SET
                value_text = excluded.value_text,
                value_integer = excluded.value_integer,
                value_real = excluded.value_real,
                value_boolean = excluded.value_boolean,
                updated_at = datetime('now')
            """,
            (
                definition_id,
                entity_id,
                value_text,
                value_integer,
                value_real,
                value_boolean,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM custom_field_values
            WHERE definition_id = ? AND entity_id = ?
            """,
            (definition_id, entity_id),
        ).fetchone()
        after = row_to_dict(row)
        write_audit(
            conn,
            action="set",
            entity_type="custom_field_value",
            entity_id=f"{definition_id}:{entity_id}",
            after_state=after,
            reason="custom field value set",
        )
        conn.commit()
        return after  # type: ignore[return-value]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_custom_field_value(
    *,
    definition_id: int,
    entity_id: str,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM custom_field_values
            WHERE definition_id = ? AND entity_id = ?
            """,
            (definition_id, entity_id),
        ).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Trace links
# ---------------------------------------------------------------------------


def create_trace_link(
    *,
    source_type: str,
    source_id: str,
    link_type: str,
    target_type: str,
    target_id: str,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        cur = conn.execute(
            """
            INSERT INTO trace_links (
                project_id, source_type, source_id, link_type, target_type, target_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                project["id"],
                source_type,
                source_id,
                link_type,
                target_type,
                target_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM trace_links WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        after = row_to_dict(row)
        write_audit(
            conn,
            action="create",
            entity_type="trace_link",
            entity_id=str(after["id"]),
            after_state=after,
            reason="trace link create",
        )
        conn.commit()
        return after  # type: ignore[return-value]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_trace_links(
    *,
    project_id: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        project = _resolve_project(conn, project_id)
        rows = conn.execute(
            """
            SELECT * FROM trace_links
            WHERE project_id = ?
            ORDER BY id ASC
            """,
            (project["id"],),
        ).fetchall()
        return [row_to_dict(r) for r in rows]  # type: ignore[misc]
    finally:
        conn.close()
