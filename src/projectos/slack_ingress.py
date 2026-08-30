"""Durable async Slack ingress: validate/dedup/enqueue/ack quickly."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from projectos.db import connection
from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.migrate import initialize_database
from projectos.services.context import ServiceContext
from projectos.slack_socket import (
    HttpPost,
    handle_events_api_payload,
    handle_slash_commands_payload,
    post_message,
)
from projectos.store import utc_now_iso

WORK_STATUS_PENDING = "pending"
WORK_STATUS_CLAIMED = "claimed"
WORK_STATUS_SUCCEEDED = "succeeded"
WORK_STATUS_FAILED = "failed"

WORK_TYPE_EVENTS_API = "events_api"
WORK_TYPE_SLASH_COMMAND = "slash_commands"


def _new_work_id() -> str:
    return f"SIW-{uuid.uuid4().hex[:10].upper()}"


def ensure_slack_ingress_table(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='slack_ingress_work'"
    ).fetchone()
    if row is None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS slack_ingress_work (
                work_id TEXT PRIMARY KEY,
                envelope_id TEXT NOT NULL,
                work_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                claimed_by TEXT,
                claim_expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_slack_ingress_envelope
                ON slack_ingress_work(envelope_id);
            CREATE INDEX IF NOT EXISTS idx_slack_ingress_status
                ON slack_ingress_work(status, created_at);
            """
        )


def enqueue_socket_work(
    conn: sqlite3.Connection,
    *,
    envelope_id: str,
    work_type: str,
    payload: dict[str, Any],
    bot_user_id: str | None = None,
) -> str | None:
    """Persist ingress work. Returns work_id or None when envelope already queued."""
    ensure_slack_ingress_table(conn)
    envelope_id = str(envelope_id or "").strip()
    if not envelope_id:
        raise OrchestrationError("Slack ingress requires envelope_id")
    existing = conn.execute(
        "SELECT work_id FROM slack_ingress_work WHERE envelope_id = ?",
        (envelope_id,),
    ).fetchone()
    if existing is not None:
        return None
    work_id = _new_work_id()
    body = {
        "payload": payload,
        "bot_user_id": bot_user_id,
    }
    conn.execute(
        """
        INSERT INTO slack_ingress_work (
            work_id, envelope_id, work_type, payload_json, status
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (work_id, envelope_id, work_type, json.dumps(body), WORK_STATUS_PENDING),
    )
    return work_id


def _claim_expiry_sql() -> str:
    return (
        "datetime(replace(replace(claim_expires_at, 'T', ' '), 'Z', '')) < datetime('now')"
    )


def claim_pending_ingress_work(
    conn: sqlite3.Connection,
    *,
    claimed_by: str,
    limit: int = 10,
    lease_seconds: int = 300,
) -> list[dict[str, Any]]:
    ensure_slack_ingress_table(conn)
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn.execute(
        f"""
        UPDATE slack_ingress_work
        SET status = ?, claimed_by = NULL, claim_expires_at = NULL
        WHERE status = ?
          AND claim_expires_at IS NOT NULL
          AND {_claim_expiry_sql()}
        """,
        (WORK_STATUS_PENDING, WORK_STATUS_CLAIMED),
    )
    rows = conn.execute(
        """
        SELECT * FROM slack_ingress_work
        WHERE status = ? AND attempts < 10
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (WORK_STATUS_PENDING, limit),
    ).fetchall()
    claimed: list[dict[str, Any]] = []
    for row in rows:
        cur = conn.execute(
            """
            UPDATE slack_ingress_work
            SET status = ?, claimed_by = ?, claim_expires_at = ?, updated_at = ?
            WHERE work_id = ? AND status = ?
            """,
            (
                WORK_STATUS_CLAIMED,
                claimed_by,
                expires,
                utc_now_iso(),
                str(row["work_id"]),
                WORK_STATUS_PENDING,
            ),
        )
        if cur.rowcount == 1:
            claimed.append(dict(row))
    return claimed


def _mark_work(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE slack_ingress_work
        SET status = ?,
            last_error = ?,
            attempts = attempts + CASE WHEN ? IN (?, ?) THEN 1 ELSE 0 END,
            updated_at = ?
        WHERE work_id = ?
        """,
        (
            status,
            (error or "")[:500] or None,
            status,
            WORK_STATUS_FAILED,
            WORK_STATUS_SUCCEEDED,
            utc_now_iso(),
            work_id,
        ),
    )


def deliver_slack_reply_via_outbox(
    conn: sqlite3.Connection,
    *,
    reply: dict[str, Any],
    channel_id: str,
    thread_ts: str | None,
    team_id: str | None,
    project_id: str | None = None,
    run_id: str | None = None,
    response_url: str | None = None,
) -> None:
    if reply.get("_outbox_delivered"):
        return
    text = str(reply.get("text") or "").strip()
    if not text and not reply.get("blocks"):
        return
    ctx = EventContext(
        project_id=project_id or "SYSTEM",
        run_id=run_id,
        slack_team_id=team_id or "",
        slack_channel_id=channel_id,
        slack_thread_ts=thread_ts or "",
    )
    metadata: dict[str, Any] = {
        "ingress_reply": True,
        "response_type": reply.get("response_type") or "in_channel",
    }
    if response_url:
        metadata["response_url"] = response_url
    if isinstance(reply.get("blocks"), list):
        metadata["blocks"] = reply["blocks"]
    emit_projectos_event(
        conn,
        ctx=ctx,
        event_type="SLACK_SPONSOR_REPLY",
        summary=text,
        actor_id=ACTOR_PM,
        phase="slack",
        metadata=metadata,
        detail_level="milestone",
    )


def _execute_work_item(
    ctx: ServiceContext,
    work: dict[str, Any],
    *,
    http_post: HttpPost | None = None,
) -> None:
    body = json.loads(str(work["payload_json"] or "{}"))
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    bot_user_id = body.get("bot_user_id")
    work_type = str(work["work_type"] or "")
    reply: dict[str, Any] | None = None
    channel_id = ""
    thread_ts: str | None = None
    response_url: str | None = None
    team_id: str | None = None

    if work_type == WORK_TYPE_SLASH_COMMAND:
        reply = handle_slash_commands_payload(ctx, payload)
        channel_id = str(payload.get("channel_id") or "")
        thread_ts = str(payload.get("thread_ts") or "").strip() or None
        response_url = str(payload.get("response_url") or "").strip() or None
        team_id = str(payload.get("team_id") or "").strip() or None
    elif work_type == WORK_TYPE_EVENTS_API:
        reply = handle_events_api_payload(
            ctx, payload, bot_user_id=bot_user_id, http_post=http_post
        )
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        channel_id = str(event.get("channel") or "")
        thread_ts = str(event.get("thread_ts") or event.get("ts") or "").strip() or None
        team_id = str(payload.get("team_id") or "").strip() or None
    else:
        raise OrchestrationError(f"Unknown slack ingress work_type {work_type!r}")

    if reply is None:
        return

    with connection(ctx.db_path) as conn:
        deliver_slack_reply_via_outbox(
            conn,
            reply=reply,
            channel_id=channel_id,
            thread_ts=thread_ts,
            team_id=team_id,
            response_url=response_url,
        )


def process_slack_ingress_batch(
    ctx: ServiceContext,
    *,
    limit: int = 10,
    claimed_by: str = "daemon",
    http_post: HttpPost | None = None,
) -> dict[str, int]:
    initialize_database(ctx.db_path)
    processed = 0
    failed = 0
    with connection(ctx.db_path) as conn:
        claimed = claim_pending_ingress_work(conn, claimed_by=claimed_by, limit=limit)
        conn.commit()

    for work in claimed:
        work_id = str(work["work_id"])
        try:
            _execute_work_item(ctx, work, http_post=http_post)
            with connection(ctx.db_path) as conn:
                _mark_work(conn, work_id=work_id, status=WORK_STATUS_SUCCEEDED)
                conn.commit()
            processed += 1
        except Exception as exc:  # noqa: BLE001
            with connection(ctx.db_path) as conn:
                _mark_work(conn, work_id=work_id, status=WORK_STATUS_FAILED, error=str(exc))
                conn.commit()
            failed += 1

    if processed or failed:
        from projectos.event_dispatcher import dispatch_event_outbox

        dispatch_event_outbox(ctx.db_path, http_post=http_post)
    return {"processed": processed, "failed": failed}


def post_ingress_reply_direct(
    *,
    reply: dict[str, Any],
    channel_id: str,
    thread_ts: str | None,
    response_url: str | None = None,
    http_post: HttpPost | None = None,
) -> None:
    """Test helper and fallback when outbox dispatch is unavailable."""
    post_message(
        channel_id=channel_id,
        text=str(reply.get("text") or ""),
        thread_ts=thread_ts,
        response_url=response_url,
        blocks=reply.get("blocks") if isinstance(reply.get("blocks"), list) else None,
        http_post=http_post,
    )
