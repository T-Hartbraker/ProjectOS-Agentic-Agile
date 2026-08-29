"""Dispatch ProjectOS domain events to subscribers."""

from __future__ import annotations

import json
from typing import Any, Callable

from projectos.db import connection
from projectos.slack_activity_blocks import activity_event_to_blocks
from projectos.slack_socket import post_message

PROJECTOS_PREFIX = "*ProjectOS:*"


def list_pending_subscriber_outbox(
    conn,
    *,
    subscriber: str = "slack",
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM event_outbox
        WHERE subscriber = ? AND status = 'pending' AND attempts < 10
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (subscriber, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_subscriber_delivered(conn, *, outbox_id: int) -> None:
    conn.execute(
        """
        UPDATE event_outbox
        SET status = 'delivered', delivered_at = datetime('now')
        WHERE id = ?
        """,
        (outbox_id,),
    )


def mark_subscriber_failed(conn, *, outbox_id: int, error: str) -> None:
    conn.execute(
        """
        UPDATE event_outbox
        SET attempts = attempts + 1,
            last_error = ?,
            status = CASE WHEN attempts + 1 >= 10 THEN 'dead' ELSE 'pending' END
        WHERE id = ?
        """,
        (str(error or "")[:500], outbox_id),
    )


def _should_project(payload: dict[str, Any], sponsor_level: str) -> bool:
    order = {"milestone": 0, "normal": 1, "verbose": 2}
    event_level = str(payload.get("detail_level") or "normal").lower()
    if order.get(event_level, 1) > order.get(sponsor_level, 1):
        return False
    return str(payload.get("visibility") or "SPONSOR") != "AUDIT"


def _slack_payload_to_blocks(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    blocks = activity_event_to_blocks(payload)
    actor = str(payload.get("actor_role") or "ProjectOS")
    run_id = str(payload.get("run_id") or "")
    summary = str(payload.get("summary") or payload.get("event_type") or "")
    prefix = actor + (f" — `{run_id}`" if run_id else "")
    text = f"{PROJECTOS_PREFIX}\n{prefix}\n{summary}"
    return text, blocks


def dispatch_event_outbox(
    db_path,
    *,
    subscriber: str = "slack",
    http_post: Callable | None = None,
    limit: int = 25,
) -> dict[str, int]:
    from projectos.agent_activity import get_activity_detail_level

    delivered = 0
    failed = 0
    with connection(db_path) as conn:
        sponsor_level = get_activity_detail_level(conn)
        pending = list_pending_subscriber_outbox(conn, subscriber=subscriber, limit=limit)

    for row in pending:
        outbox_id = int(row["id"])
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
            if subscriber == "slack":
                if not _should_project(payload, sponsor_level):
                    with connection(db_path) as conn:
                        mark_subscriber_delivered(conn, outbox_id=outbox_id)
                    delivered += 1
                    continue
                channel_id = str(payload.get("slack_channel_id") or "")
                thread_ts = str(payload.get("slack_thread_ts") or "") or None
                if not channel_id:
                    with connection(db_path) as conn:
                        mark_subscriber_delivered(conn, outbox_id=outbox_id)
                    delivered += 1
                    continue
                text, blocks = _slack_payload_to_blocks(payload)
                post_message(
                    channel_id=channel_id,
                    text=text,
                    thread_ts=thread_ts,
                    blocks=blocks,
                    http_post=http_post,
                )
            with connection(db_path) as conn:
                mark_subscriber_delivered(conn, outbox_id=outbox_id)
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            with connection(db_path) as conn:
                mark_subscriber_failed(conn, outbox_id=outbox_id, error=str(exc))
            failed += 1
    return {"delivered": delivered, "failed": failed}


def outbox_diagnostics(conn) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS cnt
        FROM event_outbox
        WHERE subscriber = 'slack'
        GROUP BY status
        """
    ).fetchall()
    return {str(row["status"]): int(row["cnt"]) for row in rows}
