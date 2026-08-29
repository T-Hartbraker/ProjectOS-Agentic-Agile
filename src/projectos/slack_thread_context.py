"""Track active ProjectOS vs ChatGPT threads in Slack."""

from __future__ import annotations

import sqlite3
from typing import Any


def mark_projectos_thread_active(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> None:
    conn.execute(
        """
        INSERT INTO slack_projectos_threads (team_id, channel_id, thread_ts, active, updated_at)
        VALUES (?, ?, ?, 1, datetime('now'))
        ON CONFLICT(team_id, channel_id, thread_ts) DO UPDATE SET
            active = 1,
            updated_at = datetime('now')
        """,
        (team_id or "", channel_id, thread_ts or ""),
    )


def is_projectos_thread_active(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> bool:
    row = conn.execute(
        """
        SELECT active FROM slack_projectos_threads
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
        """,
        (team_id or "", channel_id, thread_ts or ""),
    ).fetchone()
    return bool(row and row["active"])


def deactivate_projectos_thread(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> None:
    conn.execute(
        """
        UPDATE slack_projectos_threads
        SET active = 0, updated_at = datetime('now')
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
        """,
        (team_id or "", channel_id, thread_ts or ""),
    )


def thread_root_ts(event: dict[str, Any]) -> str:
    """Root thread timestamp for session and routing keys."""
    return str(event.get("thread_ts") or event.get("ts") or "").strip()
