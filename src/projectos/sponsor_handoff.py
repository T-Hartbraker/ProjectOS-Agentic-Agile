"""Durable SponsorHandoff domain object."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from projectos.errors import OrchestrationError

HANDOFF_STATUSES = frozenset(
    {"DRAFT", "VALIDATED", "ACCEPTED_BY_PM", "REJECTED", "SUPERSEDED"}
)


@dataclass(frozen=True)
class SponsorHandoffRecord:
    handoff_id: str
    project_id: str
    team_id: str
    channel_id: str
    thread_ts: str
    sponsor_user_id: str
    request_type: str
    objective: str
    rationale: str
    scope: str
    constraints_json: str
    acceptance_intent: str
    exclusions: str
    desired_outputs_json: str
    conversation_summary: str
    status: str
    run_id: str | None
    rejection_reason: str | None
    created_at: str
    validated_at: str | None
    accepted_at: str | None


def _row_to_record(row: sqlite3.Row) -> SponsorHandoffRecord:
    return SponsorHandoffRecord(
        handoff_id=row["handoff_id"],
        project_id=row["project_id"],
        team_id=row["team_id"] or "",
        channel_id=row["channel_id"],
        thread_ts=row["thread_ts"],
        sponsor_user_id=row["sponsor_user_id"],
        request_type=row["request_type"],
        objective=row["objective"],
        rationale=row["rationale"] or "",
        scope=row["scope"] or "",
        constraints_json=row["constraints_json"] or "{}",
        acceptance_intent=row["acceptance_intent"] or "",
        exclusions=row["exclusions"] or "",
        desired_outputs_json=row["desired_outputs_json"] or "{}",
        conversation_summary=row["conversation_summary"] or "",
        status=row["status"],
        run_id=row["run_id"],
        rejection_reason=row["rejection_reason"],
        created_at=row["created_at"],
        validated_at=row["validated_at"],
        accepted_at=row["accepted_at"],
    )


def create_sponsor_handoff(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    sponsor_user_id: str,
    request_type: str,
    objective: str,
    rationale: str = "",
    scope: str = "",
    constraints_json: str = "{}",
    acceptance_intent: str = "",
    exclusions: str = "",
    desired_outputs_json: str = "{}",
    conversation_summary: str = "",
) -> SponsorHandoffRecord:
    if not objective.strip():
        raise OrchestrationError("Sponsor handoff objective is required")
    handoff_id = f"HND-{uuid.uuid4().hex[:12].upper()}"
    conn.execute(
        """
        INSERT INTO sponsor_handoffs (
            handoff_id, project_id, team_id, channel_id, thread_ts, sponsor_user_id,
            request_type, objective, rationale, scope, constraints_json,
            acceptance_intent, exclusions, desired_outputs_json, conversation_summary,
            status, validated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'VALIDATED', datetime('now'))
        """,
        (
            handoff_id,
            project_id,
            team_id or "",
            channel_id,
            thread_ts,
            sponsor_user_id,
            request_type,
            objective[:2000],
            rationale[:2000],
            scope[:2000],
            constraints_json,
            acceptance_intent[:2000],
            exclusions[:2000],
            desired_outputs_json,
            conversation_summary[:2000],
        ),
    )
    row = conn.execute(
        "SELECT * FROM sponsor_handoffs WHERE handoff_id = ?", (handoff_id,)
    ).fetchone()
    assert row is not None
    return _row_to_record(row)


def get_sponsor_handoff(conn: sqlite3.Connection, handoff_id: str) -> SponsorHandoffRecord | None:
    row = conn.execute(
        "SELECT * FROM sponsor_handoffs WHERE handoff_id = ?", (handoff_id,)
    ).fetchone()
    return _row_to_record(row) if row else None


def get_latest_thread_handoff(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> SponsorHandoffRecord | None:
    row = conn.execute(
        """
        SELECT * FROM sponsor_handoffs
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (team_id or "", channel_id, thread_ts),
    ).fetchone()
    return _row_to_record(row) if row else None


def mark_handoff_accepted(
    conn: sqlite3.Connection,
    *,
    handoff_id: str,
    run_id: str,
) -> SponsorHandoffRecord | None:
    conn.execute(
        """
        UPDATE sponsor_handoffs
        SET status = 'ACCEPTED_BY_PM', run_id = ?, accepted_at = datetime('now')
        WHERE handoff_id = ? AND status = 'VALIDATED'
        """,
        (run_id, handoff_id),
    )
    return get_sponsor_handoff(conn, handoff_id)


def mark_handoff_rejected(
    conn: sqlite3.Connection,
    *,
    handoff_id: str,
    reason: str,
) -> None:
    conn.execute(
        """
        UPDATE sponsor_handoffs
        SET status = 'REJECTED', rejection_reason = ?
        WHERE handoff_id = ? AND status IN ('DRAFT', 'VALIDATED')
        """,
        (str(reason or "")[:500], handoff_id),
    )


def handoff_to_instruction(record: SponsorHandoffRecord) -> str:
    lines = [record.objective]
    if record.scope:
        lines.append(f"Scope: {record.scope}")
    if record.acceptance_intent:
        lines.append(f"Acceptance: {record.acceptance_intent}")
    if record.exclusions:
        lines.append(f"Exclusions: {record.exclusions}")
    try:
        desired = json.loads(record.desired_outputs_json or "{}")
        if desired:
            lines.append(f"Desired outputs: {json.dumps(desired, sort_keys=True)}")
    except json.JSONDecodeError:
        pass
    return "\n".join(lines)[:2000]
