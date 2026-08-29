"""SQLite persistence for ChatGPT Slack conversation state."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def get_chatgpt_thread(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM slack_chatgpt_threads
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
        """,
        (team_id or "", channel_id, thread_ts or ""),
    ).fetchone()
    return _thread_row(row) if row else None


def upsert_chatgpt_thread(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    sponsor_user_id: str,
    project_human_id: str | None = None,
    openai_response_id: str | None = None,
    active: bool = True,
    awaiting_projectos: bool = False,
    pending_proposal_json: str | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    conn.execute(
        """
        INSERT INTO slack_chatgpt_threads (
            team_id, channel_id, thread_ts, sponsor_user_id, project_human_id,
            openai_response_id, active, awaiting_projectos, pending_proposal_json,
            last_error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(team_id, channel_id, thread_ts) DO UPDATE SET
            sponsor_user_id = excluded.sponsor_user_id,
            project_human_id = COALESCE(excluded.project_human_id, slack_chatgpt_threads.project_human_id),
            openai_response_id = COALESCE(excluded.openai_response_id, slack_chatgpt_threads.openai_response_id),
            active = excluded.active,
            awaiting_projectos = excluded.awaiting_projectos,
            pending_proposal_json = COALESCE(excluded.pending_proposal_json, slack_chatgpt_threads.pending_proposal_json),
            last_error = excluded.last_error,
            updated_at = datetime('now')
        """,
        (
            team_id or "",
            channel_id,
            thread_ts or "",
            sponsor_user_id,
            project_human_id,
            openai_response_id,
            1 if active else 0,
            1 if awaiting_projectos else 0,
            pending_proposal_json,
            last_error,
        ),
    )
    row = get_chatgpt_thread(conn, team_id=team_id, channel_id=channel_id, thread_ts=thread_ts)
    assert row is not None
    return row


def insert_chatgpt_message(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    message_ts: str,
    user_id: str | None,
    role: str,
    text: str,
) -> bool:
    try:
        conn.execute(
            """
            INSERT INTO slack_chatgpt_messages (
                team_id, channel_id, thread_ts, message_ts, user_id, role, text
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (team_id or "", channel_id, thread_ts or "", message_ts, user_id, role, text[:8000]),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def list_chatgpt_messages(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    limit: int = 40,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT role, text, message_ts, user_id
        FROM slack_chatgpt_messages
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        (team_id or "", channel_id, thread_ts or "", limit),
    ).fetchall()
    return [
        {
            "role": row["role"],
            "text": row["text"],
            "message_ts": row["message_ts"],
            "user_id": row["user_id"],
        }
        for row in rows
    ]


def get_chatgpt_message_by_ts(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    message_ts: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT role, text, message_ts, user_id
        FROM slack_chatgpt_messages
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ? AND message_ts = ?
        """,
        (team_id or "", channel_id, thread_ts or "", message_ts),
    ).fetchone()
    if not row:
        return None
    return {
        "role": row["role"],
        "text": row["text"],
        "message_ts": row["message_ts"],
        "user_id": row["user_id"],
    }


def get_recent_chatgpt_project(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    sponsor_user_id: str,
    exclude_thread_ts: str | None = None,
) -> str | None:
    row = conn.execute(
        """
        SELECT project_human_id
        FROM slack_chatgpt_threads
        WHERE team_id = ? AND channel_id = ? AND sponsor_user_id = ?
          AND project_human_id IS NOT NULL AND TRIM(project_human_id) != ''
          AND (? IS NULL OR thread_ts != ?)
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (team_id or "", channel_id, sponsor_user_id, exclude_thread_ts, exclude_thread_ts or ""),
    ).fetchone()
    if row is None:
        return None
    return str(row["project_human_id"] or "").strip() or None


def _thread_row(row: sqlite3.Row) -> dict[str, Any]:
    pending = row["pending_proposal_json"]
    proposal = None
    if pending:
        try:
            proposal = json.loads(pending)
        except json.JSONDecodeError:
            proposal = None
    return {
        "team_id": row["team_id"] or "",
        "channel_id": row["channel_id"],
        "thread_ts": row["thread_ts"] or "",
        "sponsor_user_id": row["sponsor_user_id"],
        "project_human_id": row["project_human_id"],
        "openai_response_id": row["openai_response_id"],
        "active": bool(row["active"]),
        "awaiting_projectos": bool(row["awaiting_projectos"]),
        "pending_proposal": proposal,
        "last_error": row["last_error"],
    }
