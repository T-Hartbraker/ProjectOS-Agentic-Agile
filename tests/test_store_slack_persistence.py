"""Slack persistence API tests (store.py authoritative helpers)."""

from __future__ import annotations

from pathlib import Path

from helpers import write_registry
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.store import (
    delete_slack_binding,
    get_slack_binding,
    insert_slack_binding,
    insert_slack_message_ref,
    list_slack_bindings_for_project,
    list_slack_message_refs_for_project,
)


def test_slack_binding_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "projectos.db"
    initialize_database(db)
    write_registry(tmp_path / "projects.json", [])
    with connection(db) as conn:
        inserted = insert_slack_binding(
            conn,
            binding_human_id="BIND-1",
            project_human_id="PRJ-003",
            team_id="T1",
            channel_id="C1",
            thread_ts="1.0",
        )
        fetched = get_slack_binding(conn, team_id="T1", channel_id="C1", thread_ts="1.0")
        listed = list_slack_bindings_for_project(conn, "PRJ-003")
        delete_slack_binding(conn, team_id="T1", channel_id="C1", thread_ts="1.0")
        after_delete = get_slack_binding(conn, team_id="T1", channel_id="C1", thread_ts="1.0")
    assert inserted["binding_human_id"] == "BIND-1"
    assert fetched is not None
    assert len(listed) == 1
    assert after_delete is None


def test_slack_message_ref_listing(tmp_path: Path) -> None:
    db = tmp_path / "projectos.db"
    initialize_database(db)
    write_registry(tmp_path / "projects.json", [])
    with connection(db) as conn:
        insert_slack_message_ref(
            conn,
            project_human_id="PRJ-003",
            team_id="T1",
            channel_id="C1",
            thread_ts="1.0",
            message_ts="2.0",
        )
        refs = list_slack_message_refs_for_project(conn, "PRJ-003")
    assert len(refs) == 1
    assert refs[0]["message_ts"] == "2.0"
