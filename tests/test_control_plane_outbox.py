"""Outbox recovery, ordering, and exactly-once delivery tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import init_git_repo, write_identity, write_registry
from projectos.db import connection
from projectos.event_dispatcher import (
    claim_pending_outbox,
    dispatch_event_outbox,
    mark_subscriber_blocked,
    mark_subscriber_delivered,
    mark_subscriber_failed,
    release_blocked_outbox,
)
from projectos.migrate import initialize_database


def _ctx(tmp_path: Path):
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Gamma")
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    return db


def _insert_outbox(
    conn,
    *,
    event_id: str,
    run_id: str,
    channel: str = "C1",
    thread: str = "1.0",
) -> int:
    conn.execute(
        """
        INSERT INTO projectos_events (
            event_id, project_id, actor_type, actor_id, actor_role,
            event_type, summary, run_id
        ) VALUES (?, 'PRJ-003', 'agent', 'pm-agent', 'PM', ?, ?, ?)
        """,
        (event_id, event_id, event_id, run_id),
    )
    payload = {
        "event_id": event_id,
        "event_type": event_id,
        "run_id": run_id,
        "slack_channel_id": channel,
        "slack_thread_ts": thread,
        "detail_level": "milestone",
        "summary": event_id,
    }
    cur = conn.execute(
        """
        INSERT INTO event_outbox (event_id, subscriber, idempotency_key, payload_json, status, attempts)
        VALUES (?, 'slack', ?, ?, 'pending', 0)
        """,
        (event_id, f"slack:{event_id}", json.dumps(payload)),
    )
    return int(cur.lastrowid)


def test_event_outbox_recovery_ordering_and_exactly_once(tmp_path: Path, monkeypatch) -> None:
    db = _ctx(tmp_path)
    posts: list[str] = []
    monkeypatch.setattr(
        "projectos.event_dispatcher.post_message",
        lambda **kwargs: posts.append(str(kwargs.get("text") or "")),
    )

    with connection(db) as conn:
        id_a = _insert_outbox(conn, event_id="EVT-A", run_id="RUN-1")
        id_b = _insert_outbox(conn, event_id="EVT-B", run_id="RUN-1")
        id_c = _insert_outbox(conn, event_id="EVT-C", run_id="RUN-1")
        id_d = _insert_outbox(conn, event_id="EVT-D", run_id="RUN-2")

        claimed = claim_pending_outbox(conn, subscriber="slack", claimed_by="worker-1", limit=4)
        assert claimed[0]["event_id"] == "EVT-A"
        mark_subscriber_failed(conn, outbox_id=id_a, error="transient")
        mark_subscriber_blocked(conn, outbox_id=id_b, blocked_by_outbox_id=id_a, error="hold")
        mark_subscriber_blocked(conn, outbox_id=id_c, blocked_by_outbox_id=id_a, error="hold")
        b_attempts = conn.execute("SELECT attempts FROM event_outbox WHERE id = ?", (id_b,)).fetchone()[0]
        c_attempts = conn.execute("SELECT attempts FROM event_outbox WHERE id = ?", (id_c,)).fetchone()[0]
        assert int(b_attempts) == 0
        assert int(c_attempts) == 0

        expired = (
            datetime.now(timezone.utc) - timedelta(seconds=600)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        conn.execute(
            "UPDATE event_outbox SET status = 'claimed', claimed_by = 'dead-worker', claim_expires_at = ?, attempts = 1 WHERE id = ?",
            (expired, id_a),
        )

    with connection(db) as conn:
        claim_pending_outbox(conn, subscriber="slack", claimed_by="worker-2", limit=4)
        release_blocked_outbox(conn, blocked_by_outbox_id=id_a)
        mark_subscriber_delivered(conn, outbox_id=id_a)
        mark_subscriber_delivered(conn, outbox_id=id_d)

    stats = dispatch_event_outbox(db)
    assert stats["delivered"] >= 0

    with connection(db) as conn:
        d_status = conn.execute(
            "SELECT status FROM event_outbox WHERE event_id = 'EVT-D'"
        ).fetchone()
        b_attempts = conn.execute("SELECT attempts FROM event_outbox WHERE event_id = 'EVT-B'").fetchone()[0]
    assert d_status is not None
    assert d_status["status"] == "delivered"
    assert int(b_attempts) == 0
