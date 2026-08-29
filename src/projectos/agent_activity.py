"""Agent activity events and Slack outbox enqueue."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

DETAIL_LEVELS = frozenset({"milestone", "normal", "verbose"})
VISIBILITY_LEVELS = frozenset({"INTERNAL", "SPONSOR", "AUDIT_ONLY"})


@dataclass(frozen=True)
class AgentActivityEvent:
    event_id: str
    project_id: str
    run_id: str | None
    event_type: str
    summary: str
    actor_role: str
    detail_level: str
    visibility: str


def get_activity_detail_level(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT activity_detail_level FROM slack_cockpit_settings WHERE id = 1"
    ).fetchone()
    level = str(row["activity_detail_level"] if row else "normal").lower()
    return level if level in DETAIL_LEVELS else "normal"


def _level_visible(sponsor_level: str, event_level: str) -> bool:
    order = {"milestone": 0, "normal": 1, "verbose": 2}
    return order.get(event_level, 1) <= order.get(sponsor_level, 1)


@dataclass(frozen=True)
class ThreadCorrelation:
    project_id: str
    handoff_id: str | None
    run_id: str | None
    team_id: str
    channel_id: str
    thread_ts: str


def lookup_thread_correlation(
    conn: sqlite3.Connection,
    *,
    run_id: str | None = None,
    project_id: str | None = None,
) -> ThreadCorrelation | None:
    if run_id:
        row = conn.execute(
            """
            SELECT h.project_id, h.handoff_id, h.run_id, h.team_id, h.channel_id, h.thread_ts
            FROM sponsor_handoffs h
            WHERE h.run_id = ?
            ORDER BY h.created_at DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    elif project_id:
        row = conn.execute(
            """
            SELECT h.project_id, h.handoff_id, h.run_id, h.team_id, h.channel_id, h.thread_ts
            FROM execution_runs r
            JOIN sponsor_handoffs h ON h.handoff_id = r.handoff_id
            WHERE r.project_id = ?
              AND r.status IN ('PLANNING', 'WAITING_APPROVAL', 'WAITING_FOR_SPONSOR', 'RUNNING', 'BLOCKED')
            ORDER BY r.created_at DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
    else:
        return None
    if not row:
        return None
    return ThreadCorrelation(
        project_id=str(row["project_id"]),
        handoff_id=str(row["handoff_id"]) if row["handoff_id"] else None,
        run_id=str(row["run_id"]) if row["run_id"] else None,
        team_id=str(row["team_id"] or ""),
        channel_id=str(row["channel_id"]),
        thread_ts=str(row["thread_ts"]),
    )


def _correlation_metadata(thread: ThreadCorrelation | None) -> dict[str, Any]:
    if thread is None:
        return {}
    return {
        "project_id": thread.project_id,
        "handoff_id": thread.handoff_id,
        "run_id": thread.run_id,
        "slack_team_id": thread.team_id,
        "slack_channel_id": thread.channel_id,
        "slack_thread_ts": thread.thread_ts,
    }


def record_sponsor_activity(
    conn: sqlite3.Connection,
    *,
    thread: ThreadCorrelation,
    event_type: str,
    summary: str,
    actor_role: str,
    actor_id: str = "pm",
    detail: str = "",
    evidence: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    visibility: str = "SPONSOR",
    detail_level: str = "normal",
    work_item_id: str | None = None,
    release_id: str | None = None,
    project_to_slack: bool = True,
) -> AgentActivityEvent:
    """Persist audit-only legacy activity; Sponsor Slack uses event_outbox."""
    merged_meta = {**_correlation_metadata(thread), **(metadata or {})}
    evt = emit_agent_activity(
        conn,
        project_id=thread.project_id,
        run_id=thread.run_id,
        event_type=event_type,
        summary=summary,
        actor_id=actor_id,
        actor_role=actor_role,
        detail=detail,
        evidence=evidence,
        metadata=merged_meta,
        visibility=visibility,
        detail_level=detail_level,
        work_item_id=work_item_id,
        release_id=release_id,
    )
    if project_to_slack and thread.run_id:
        from projectos.domain_events import EventContext, emit_projectos_event

        emit_projectos_event(
            conn,
            ctx=EventContext(
                project_id=thread.project_id,
                handoff_id=thread.handoff_id,
                run_id=thread.run_id,
                slack_team_id=thread.team_id,
                slack_channel_id=thread.channel_id,
                slack_thread_ts=thread.thread_ts,
            ),
            event_type=event_type,
            summary=summary,
            actor_id=actor_id,
            actor_role=actor_role,
            detail=detail,
            evidence=evidence,
            metadata=merged_meta,
            visibility=visibility,
            detail_level=detail_level,
        )
    return evt


def emit_agent_activity(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    run_id: str | None,
    event_type: str,
    summary: str,
    actor_type: str = "agent",
    actor_id: str = "pm",
    actor_role: str = "PM Agent",
    detail: str = "",
    phase: str = "",
    status: str = "",
    progress_percent: int | None = None,
    evidence: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    visibility: str = "SPONSOR",
    detail_level: str = "normal",
    work_item_id: str | None = None,
    release_id: str | None = None,
) -> AgentActivityEvent:
    event_id = f"EVT-{uuid.uuid4().hex[:12].upper()}"
    conn.execute(
        """
        INSERT INTO agent_activity_events (
            event_id, project_id, run_id, work_item_id, release_id,
            actor_type, actor_id, actor_role, event_type, severity,
            phase, status, progress_percent, summary, detail,
            evidence_json, metadata_json, visibility, detail_level
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'info', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            project_id,
            run_id,
            work_item_id,
            release_id,
            actor_type,
            actor_id,
            actor_role,
            event_type,
            phase,
            status,
            progress_percent,
            summary[:1000],
            detail[:4000] if detail else None,
            json.dumps(evidence, sort_keys=True) if evidence else None,
            json.dumps(metadata, sort_keys=True) if metadata else None,
            visibility if visibility in VISIBILITY_LEVELS else "SPONSOR",
            detail_level if detail_level in DETAIL_LEVELS else "normal",
        ),
    )
    return AgentActivityEvent(
        event_id=event_id,
        project_id=project_id,
        run_id=run_id,
        event_type=event_type,
        summary=summary,
        actor_role=actor_role,
        detail_level=detail_level,
        visibility=visibility,
    )


def enqueue_slack_activity(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> int:
    """Retired: Sponsor Slack delivery uses canonical event_outbox only."""
    return 0


def list_pending_outbox(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM slack_activity_outbox
        WHERE status = 'pending' AND attempts < 10
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_outbox_delivered(conn: sqlite3.Connection, *, outbox_id: int) -> None:
    conn.execute(
        """
        UPDATE slack_activity_outbox
        SET status = 'delivered', delivered_at = datetime('now')
        WHERE id = ?
        """,
        (outbox_id,),
    )


def mark_outbox_failed(conn: sqlite3.Connection, *, outbox_id: int, error: str) -> None:
    conn.execute(
        """
        UPDATE slack_activity_outbox
        SET attempts = attempts + 1,
            last_error = ?,
            status = CASE WHEN attempts + 1 >= 10 THEN 'dead' ELSE 'pending' END
        WHERE id = ?
        """,
        (str(error or "")[:500], outbox_id),
    )


def outbox_diagnostics(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS cnt
        FROM slack_activity_outbox
        GROUP BY status
        """
    ).fetchall()
    return {str(row["status"]): int(row["cnt"]) for row in rows}


def enqueue_activity_projection(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    event_id: str,
    event_payload: dict[str, Any],
) -> int:
    """Retired: use emit_projectos_event() + event_outbox."""
    return 0
