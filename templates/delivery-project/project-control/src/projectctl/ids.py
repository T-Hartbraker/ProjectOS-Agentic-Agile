"""Deterministic human-readable ID generation from persisted state."""

from __future__ import annotations

import re
import sqlite3

# Entity key -> (table, prefix)
ENTITY_ID_SPECS: dict[str, tuple[str, str]] = {
    "project": ("projects", "PRJ"),
    "requirement": ("requirements", "REQ"),
    "acceptance_criterion": ("acceptance_criteria", "AC"),
    "epic": ("epics", "EPIC"),
    "feature": ("features", "FEAT"),
    "story": ("stories", "US"),
    "task": ("tasks", "TASK"),
    "iteration": ("iterations", "ITER"),
    "release": ("releases", "REL"),
    "defect": ("defects", "BUG"),
    "test_case": ("test_cases", "TEST"),
    "test_run": ("test_runs", "TRUN"),
    "risk": ("risks", "RISK"),
    "issue": ("issues", "ISSUE"),
    "assumption": ("assumptions", "ASM"),
    "decision": ("decisions", "DEC"),
    "change_request": ("change_requests", "CR"),
    "agent": ("agents", "AGENT"),
    "agent_run": ("agent_runs", "RUN"),
    "artifact": ("artifacts", "ART"),
    "improvement": ("improvements", "IMP"),
}

_SUFFIX_RE = re.compile(r"^([A-Z]+)-(\d+)$")


def next_human_id(conn: sqlite3.Connection, entity_key: str) -> str:
    """Return the next human-readable ID for an entity type.

    Sequence is derived from the maximum numeric suffix already stored
    for that prefix in the corresponding table.
    """
    if entity_key not in ENTITY_ID_SPECS:
        raise KeyError(f"Unknown entity key for ID generation: {entity_key}")
    table, prefix = ENTITY_ID_SPECS[entity_key]
    return next_id_for_table(conn, table=table, prefix=prefix)


def next_id_for_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    prefix: str,
    column: str = "human_id",
) -> str:
    pattern = f"{prefix}-%"
    rows = conn.execute(
        f"SELECT {column} AS hid FROM {table} WHERE {column} LIKE ?",
        (pattern,),
    ).fetchall()
    max_n = 0
    for row in rows:
        hid = row["hid"] if isinstance(row, sqlite3.Row) else row[0]
        match = _SUFFIX_RE.match(str(hid))
        if match and match.group(1) == prefix:
            max_n = max(max_n, int(match.group(2)))
    return f"{prefix}-{max_n + 1:03d}"
