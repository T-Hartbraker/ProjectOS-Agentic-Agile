"""Canonical ProjectOS domain events and transactional outbox."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

EVENT_VERSION = 1
VISIBILITY_LEVELS = frozenset({"INTERNAL", "SPONSOR", "AUDIT"})
DETAIL_LEVELS = frozenset({"milestone", "normal", "verbose"})

# Stable logical actor identities
ACTOR_PM = "pm-agent"
ACTOR_ARCHITECTURE = "architecture-agent"
ACTOR_DEVELOPER = "developer-agent"
ACTOR_QA = "qa-agent"
ACTOR_SECURITY = "security-agent"
ACTOR_ASSURANCE = "assurance-agent"
ACTOR_DELIVERY = "delivery-agent"
ACTOR_RELEASE = "release-agent"
ACTOR_IMPROVEMENT = "improvement-agent"

ACTOR_ROLE_LABELS = {
    ACTOR_PM: "PM Agent",
    ACTOR_ARCHITECTURE: "Architecture Agent",
    ACTOR_DEVELOPER: "Developer Agent",
    ACTOR_QA: "QA Agent",
    ACTOR_SECURITY: "Security Agent",
    ACTOR_ASSURANCE: "QC Agent",
    ACTOR_DELIVERY: "Delivery Agent",
    ACTOR_RELEASE: "Release Agent",
    ACTOR_IMPROVEMENT: "Improvement Agent",
}


@dataclass(frozen=True)
class EventContext:
    project_id: str
    handoff_id: str | None = None
    run_id: str | None = None
    iteration_id: str | None = None
    job_id: str | None = None
    work_item_id: str | None = None
    release_id: str | None = None
    release_record_id: str | None = None
    artifact_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    slack_team_id: str = ""
    slack_channel_id: str = ""
    slack_thread_ts: str = ""


@dataclass(frozen=True)
class ProjectOSEvent:
    event_id: str
    project_id: str
    event_type: str
    summary: str
    actor_id: str
    actor_role: str


def _new_event_id() -> str:
    return f"EVT-{uuid.uuid4().hex[:12].upper()}"


def emit_projectos_event(
    conn: sqlite3.Connection,
    *,
    ctx: EventContext,
    event_type: str,
    summary: str,
    actor_type: str = "agent",
    actor_id: str = ACTOR_PM,
    actor_role: str | None = None,
    phase: str = "",
    status: str = "",
    severity: str = "info",
    progress: int | None = None,
    detail: str = "",
    evidence: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    visibility: str = "SPONSOR",
    detail_level: str = "normal",
    subscribers: tuple[str, ...] = ("slack",),
) -> ProjectOSEvent:
    """Persist canonical event and subscriber outbox rows in the caller's transaction."""
    from projectos.event_truthfulness import validate_event_truthfulness

    validate_event_truthfulness(
        conn,
        event_type=event_type,
        ctx=ctx,
        evidence=evidence,
        metadata=metadata,
    )
    event_id = _new_event_id()
    role = actor_role or ACTOR_ROLE_LABELS.get(actor_id, actor_id)
    merged_meta: dict[str, Any] = {}
    if ctx.slack_channel_id:
        merged_meta.update(
            {
                "slack_team_id": ctx.slack_team_id,
                "slack_channel_id": ctx.slack_channel_id,
                "slack_thread_ts": ctx.slack_thread_ts,
            }
        )
    if metadata:
        merged_meta.update(metadata)

    conn.execute(
        """
        INSERT INTO projectos_events (
            event_id, event_version, project_id, handoff_id, run_id, iteration_id,
            job_id, work_item_id, release_id, release_record_id, artifact_id,
            actor_type, actor_id, actor_role, event_type, phase, status, severity,
            progress, summary, detail, evidence_json, metadata_json,
            visibility, detail_level, correlation_id, causation_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            EVENT_VERSION,
            ctx.project_id,
            ctx.handoff_id,
            ctx.run_id,
            ctx.iteration_id,
            ctx.job_id,
            ctx.work_item_id,
            ctx.release_id,
            ctx.release_record_id,
            ctx.artifact_id,
            actor_type,
            actor_id,
            role,
            event_type,
            phase or None,
            status or None,
            severity,
            progress,
            summary[:1000],
            detail[:4000] if detail else None,
            json.dumps(evidence, sort_keys=True) if evidence else None,
            json.dumps(merged_meta, sort_keys=True) if merged_meta else None,
            visibility if visibility in VISIBILITY_LEVELS else "SPONSOR",
            detail_level if detail_level in DETAIL_LEVELS else "normal",
            ctx.correlation_id or ctx.run_id,
            ctx.causation_id,
        ),
    )
    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "project_id": ctx.project_id,
        "handoff_id": ctx.handoff_id,
        "run_id": ctx.run_id,
        "summary": summary,
        "detail": detail,
        "actor_id": actor_id,
        "actor_role": role,
        "detail_level": detail_level,
        "visibility": visibility,
        "evidence": evidence,
        "slack_team_id": ctx.slack_team_id,
        "slack_channel_id": ctx.slack_channel_id,
        "slack_thread_ts": ctx.slack_thread_ts,
    }
    if merged_meta:
        payload["metadata"] = merged_meta
        for key in ("request_type", "phases", "options", "assigned_agent", "agent_id"):
            if key in merged_meta:
                payload[key] = merged_meta[key]
    for subscriber in subscribers:
        if visibility == "AUDIT" and subscriber == "slack":
            continue
        conn.execute(
            """
            INSERT INTO event_outbox (
                event_id, subscriber, idempotency_key, payload_json, status
            ) VALUES (?, ?, ?, ?, 'pending')
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                event_id,
                subscriber,
                f"{subscriber}:{event_id}",
                json.dumps(payload, sort_keys=True),
            ),
        )
    if ctx.run_id:
        from projectos.run_state import apply_event_to_run

        apply_event_to_run(
            conn,
            run_id=ctx.run_id,
            event_type=event_type,
            payload={
                "status": status,
                "phase": phase or event_type,
                "actor_id": actor_id,
                "progress": progress,
                "summary": summary,
                "evidence": evidence,
            },
        )
        from projectos.run_evidence import maybe_close_run_after_event

        maybe_close_run_after_event(conn, event_ctx=ctx, event_type=event_type)
    return ProjectOSEvent(
        event_id=event_id,
        project_id=ctx.project_id,
        event_type=event_type,
        summary=summary,
        actor_id=actor_id,
        actor_role=role,
    )


def list_recent_events(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    run_id: str | None = None,
    visibility: str = "SPONSOR",
    limit: int = 20,
) -> list[dict[str, Any]]:
    if run_id:
        rows = conn.execute(
            """
            SELECT * FROM projectos_events
            WHERE project_id = ? AND run_id = ?
              AND visibility IN ('SPONSOR', 'AUDIT')
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (project_id, run_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM projectos_events
            WHERE project_id = ?
              AND visibility IN ('SPONSOR', 'AUDIT')
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def event_context_from_thread(
    *,
    project_id: str,
    handoff_id: str | None,
    run_id: str | None,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> EventContext:
    return EventContext(
        project_id=project_id,
        handoff_id=handoff_id,
        run_id=run_id,
        correlation_id=run_id,
        slack_team_id=team_id or "",
        slack_channel_id=channel_id,
        slack_thread_ts=thread_ts,
    )


def lookup_event_context_for_job(
    conn: sqlite3.Connection, job_id: int
) -> EventContext | None:
    from projectos.store import get_job

    try:
        resolved_id = int(job_id)
    except (TypeError, ValueError):
        return None
    job = get_job(conn, resolved_id)
    run_id = job.run_id
    if not run_id:
        row = conn.execute(
            """
            SELECT run_id FROM qa_evidence
            WHERE assurance_job_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if row and row["run_id"]:
            run_id = str(row["run_id"])
    if not run_id:
        row = conn.execute(
            """
            SELECT run_id FROM remediation_work
            WHERE orchestration_job_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if row and row["run_id"]:
            run_id = str(row["run_id"])
    if not run_id:
        return lookup_event_context_for_project(conn, job.project_human_id)
    row = conn.execute(
        """
        SELECT h.project_id, h.handoff_id, r.run_id, h.team_id, h.channel_id, h.thread_ts
        FROM execution_runs r
        JOIN sponsor_handoffs h ON h.handoff_id = r.handoff_id
        WHERE r.run_id = ?
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if not row:
        return EventContext(project_id=job.project_human_id, run_id=run_id)
    return EventContext(
        project_id=str(row["project_id"]),
        handoff_id=str(row["handoff_id"]) if row["handoff_id"] else None,
        run_id=str(row["run_id"]) if row["run_id"] else None,
        job_id=job.human_id,
        work_item_id=job.work_item_human_id,
        correlation_id=str(row["run_id"]) if row["run_id"] else None,
        slack_team_id=str(row["team_id"] or ""),
        slack_channel_id=str(row["channel_id"]),
        slack_thread_ts=str(row["thread_ts"]),
    )


def lookup_event_context_for_project(
    conn: sqlite3.Connection, project_id: str
) -> EventContext | None:
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
    if not row:
        return None
    return EventContext(
        project_id=str(row["project_id"]),
        handoff_id=str(row["handoff_id"]) if row["handoff_id"] else None,
        run_id=str(row["run_id"]) if row["run_id"] else None,
        correlation_id=str(row["run_id"]) if row["run_id"] else None,
        slack_team_id=str(row["team_id"] or ""),
        slack_channel_id=str(row["channel_id"]),
        slack_thread_ts=str(row["thread_ts"]),
    )
