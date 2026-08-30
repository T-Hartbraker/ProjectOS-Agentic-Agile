"""Audit logging for material state mutations."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def _serialize(state: Any) -> str | None:
    if state is None:
        return None
    if isinstance(state, str):
        return state
    return json.dumps(state, sort_keys=True, default=str)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def write_audit(
    conn: sqlite3.Connection,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    before_state: Any = None,
    after_state: Any = None,
    reason: str | None = None,
    actor_type: str | None = "cli",
    actor_id: str | None = None,
    agent_run_id: str | None = None,
) -> int:
    """Insert an audit_log row and return its rowid."""
    cur = conn.execute(
        """
        INSERT INTO audit_log (
            actor_type, actor_id, action, entity_type, entity_id,
            before_state, after_state, reason, agent_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor_type,
            actor_id,
            action,
            entity_type,
            entity_id,
            _serialize(before_state),
            _serialize(after_state),
            reason,
            agent_run_id,
        ),
    )
    return int(cur.lastrowid)
