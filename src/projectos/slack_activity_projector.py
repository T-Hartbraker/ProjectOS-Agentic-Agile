"""Slack activity projection — canonical event_outbox dispatcher."""

from __future__ import annotations

from typing import Callable


def flush_slack_activity_outbox(
    db_path,
    *,
    http_post: Callable | None = None,
    limit: int = 25,
) -> dict[str, int]:
    """Dispatch canonical ProjectOS events to Slack (legacy outbox retired)."""
    from projectos.event_dispatcher import dispatch_event_outbox

    return dispatch_event_outbox(db_path, subscriber="slack", http_post=http_post, limit=limit)
