"""Project CRUD, IDs, audit, status, custom fields, and trace tests."""

from __future__ import annotations

from pathlib import Path

from projectctl.db import connect
from projectctl.migrate import initialize_database
from projectctl import store


def test_project_creation_succeeds(db_path: Path) -> None:
    row = store.create_project("Alpha", db_path=db_path)
    assert row["name"] == "Alpha"
    assert row["human_id"].startswith("PRJ-")


def test_human_readable_prj_ids_are_generated(db_path: Path) -> None:
    row = store.create_project("One", db_path=db_path)
    assert row["human_id"] == "PRJ-001"


def test_multiple_projects_receive_unique_sequential_ids(db_path: Path) -> None:
    a = store.create_project("A", db_path=db_path)
    b = store.create_project("B", db_path=db_path)
    c = store.create_project("C", db_path=db_path)
    assert [a["human_id"], b["human_id"], c["human_id"]] == [
        "PRJ-001",
        "PRJ-002",
        "PRJ-003",
    ]


def test_project_creation_writes_audit_record(db_path: Path) -> None:
    store.create_project("Audited", db_path=db_path)
    conn = connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM audit_log
            WHERE entity_type = 'project' AND action = 'create'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        assert row is not None
        assert row["entity_id"] == "PRJ-001"
        assert row["after_state"] is not None
    finally:
        conn.close()


def test_project_list_works(db_path: Path) -> None:
    store.create_project("List Me", db_path=db_path)
    rows = store.list_projects(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["human_id"] == "PRJ-001"


def test_project_show_works(db_path: Path) -> None:
    store.create_project("Show Me", db_path=db_path)
    row = store.show_project("PRJ-001", db_path=db_path)
    assert row["name"] == "Show Me"
    assert row["human_id"] == "PRJ-001"


def test_status_works_with_no_project(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    initialize_database(db_path=db)
    status = store.get_status(db_path=db)
    assert status["initialized"] is True
    assert status["active_project"] is None
    assert "No active project" in status["message"]


def test_status_works_with_no_database(tmp_path: Path) -> None:
    db = tmp_path / "missing.db"
    status = store.get_status(db_path=db)
    assert status["initialized"] is False
    assert "No project database" in status["message"]


def test_status_works_with_a_project(db_path: Path) -> None:
    store.create_project("Status Project", db_path=db_path)
    status = store.get_status(db_path=db_path)
    assert status["active_project"]["human_id"] == "PRJ-001"
    assert "PRJ-001" in status["message"]
    assert "requirements" in status["counts"]


def test_custom_field_creation_and_value_storage(db_path: Path) -> None:
    project = store.create_project("CF Project", db_path=db_path)
    definition = store.create_custom_field_definition(
        entity_type="project",
        field_key="sponsor",
        display_name="Sponsor",
        data_type="text",
        db_path=db_path,
    )
    assert definition["data_type"] == "text"
    value = store.set_custom_field_value(
        definition_id=definition["id"],
        entity_id=project["human_id"],
        value="Sponsor Name",
        db_path=db_path,
    )
    assert value["value_text"] == "Sponsor Name"
    loaded = store.get_custom_field_value(
        definition_id=definition["id"],
        entity_id=project["human_id"],
        db_path=db_path,
    )
    assert loaded is not None
    assert loaded["value_text"] == "Sponsor Name"


def test_trace_link_creation_works(db_path: Path) -> None:
    store.create_project("Trace Project", db_path=db_path)
    req = store.create_requirement("Need feature", db_path=db_path)
    story = store.create_story("Implement feature", db_path=db_path)
    link = store.create_trace_link(
        source_type="requirement",
        source_id=req["human_id"],
        link_type="IMPLEMENTED_BY",
        target_type="story",
        target_id=story["human_id"],
        db_path=db_path,
    )
    assert link["link_type"] == "IMPLEMENTED_BY"
    assert link["source_id"] == req["human_id"]
    assert link["target_id"] == story["human_id"]
    listed = store.list_trace_links(db_path=db_path)
    assert len(listed) == 1
