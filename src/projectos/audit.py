"""Project-scoped audit projection. Does not rewrite source records."""

from __future__ import annotations

from typing import Any

from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.services.context import ServiceContext
from projectos.services.facades import RegistryService
from projectos.store import require_safe_id


def list_audit(
    ctx: ServiceContext,
    project_human_id: str,
    *,
    actor_type: str | None = None,
    action: str | None = None,
    entity_kind: str | None = None,
    entity_human_id: str | None = None,
    iteration_human_id: str | None = None,
    source: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    RegistryService(ctx).show(project)
    initialize_database(ctx.db_path)
    cap = max(1, min(int(limit), 300))
    with connection(ctx.db_path) as conn:
        rows = conn.execute(
            """
            SELECT created_at, actor_type, actor_id, action, entity_kind,
                   entity_human_id, iteration_human_id, source
            FROM (
                SELECT e.created_at AS created_at,
                       'orchestration' AS actor_type,
                       e.event_type AS actor_id,
                       e.event_type AS action,
                       'job' AS entity_kind,
                       j.human_id AS entity_human_id,
                       j.iteration_human_id AS iteration_human_id,
                       'orchestration' AS source
                FROM run_events e
                INNER JOIN orchestration_jobs j ON j.id = e.job_id
                WHERE j.project_human_id = ?
                UNION ALL
                SELECT created_at, 'learning', COALESCE(actor, 'system'), event_type,
                       'memory', memory_human_id, NULL, 'learning'
                FROM agent_memory_events
                WHERE project_human_id = ?
                UNION ALL
                SELECT created_at, 'approval', COALESCE(actor, 'system'), event_type,
                       'decision', decision_human_id, NULL, 'approval'
                FROM governance_decision_events
                WHERE project_human_id = ?
                UNION ALL
                SELECT created_at, 'slack', channel_id, 'slack_message',
                       'slack_message', message_ts, NULL, 'slack'
                FROM slack_message_refs
                WHERE project_human_id = ?
                UNION ALL
                SELECT created_at, 'slack', channel_id, 'slack_intake',
                       item_kind, item_human_id, NULL, 'slack'
                FROM slack_intake_items
                WHERE project_human_id = ?
                UNION ALL
                SELECT created_at, 'slack', channel_id, kind,
                       'notification', entity_human_id, NULL, 'slack'
                FROM slack_notifications
                WHERE project_human_id = ?
            )
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (project, project, project, project, project, project, cap * 4),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "occurred_at": str(row["created_at"]),
            "actor_type": str(row["actor_type"]),
            "actor_id": str(row["actor_id"] or "system"),
            "action": str(row["action"]),
            "entity_kind": str(row["entity_kind"]),
            "entity_human_id": str(row["entity_human_id"]),
            "iteration_human_id": row["iteration_human_id"],
            "source": str(row["source"]),
        }
        if actor_type and item["actor_type"] != actor_type:
            continue
        if action and item["action"] != action:
            continue
        if entity_kind and item["entity_kind"] != entity_kind:
            continue
        if entity_human_id and item["entity_human_id"] != entity_human_id:
            continue
        if iteration_human_id and item["iteration_human_id"] != iteration_human_id:
            continue
        if source and item["source"] != source:
            continue
        events.append(item)
        if len(events) >= cap:
            break
    return {
        "project_human_id": project,
        "notice": "Audit explorer is a projection over source records; it does not replace them.",
        "events": events,
    }
