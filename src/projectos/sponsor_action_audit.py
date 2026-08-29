"""Audit trail for Sponsor action requests through the Advisor bridge."""

from __future__ import annotations

import sqlite3
from typing import Any

from projectos.advisor_errors import AdvisorError, new_error_id


def record_sponsor_action_audit(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    sponsor_user_id: str,
    message_text: str,
    project_human_id: str | None = None,
    project_resolution: str | None = None,
    action_intent: str | None = None,
    handoff_attempted: bool = False,
    failure_stage: str | None = None,
    error: AdvisorError | None = None,
    error_id: str | None = None,
    pm_reached: bool = False,
    mutation_occurred: bool = False,
) -> str:
    eid = error_id or new_error_id()
    conn.execute(
        """
        INSERT INTO sponsor_action_audit (
            error_id, team_id, channel_id, thread_ts, sponsor_user_id,
            project_human_id, message_text, project_resolution, action_intent,
            handoff_attempted, failure_stage, error_class, error_detail,
            pm_reached, mutation_occurred
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            eid,
            team_id or "",
            channel_id,
            thread_ts or "",
            sponsor_user_id or "",
            project_human_id,
            (message_text or "")[:4000],
            project_resolution,
            action_intent,
            1 if handoff_attempted else 0,
            failure_stage,
            error.error_class if error else None,
            error.detail[:500] if error else None,
            1 if pm_reached else 0,
            1 if mutation_occurred else 0,
        ),
    )
    return eid
