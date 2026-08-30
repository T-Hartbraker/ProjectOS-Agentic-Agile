"""Work-item bootstrap ordering, identity mapping, and partial-failure safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers import write_registry
from projectos.db import connection
from projectos.errors import OrchestrationError
from projectos.migrate import initialize_database
from projectos.plan import run_plan
from projectos.project_creation import bootstrap_project_repository, validate_project_control_state
from projectos.projectctl_bridge import create_projectctl_entity, read_work_item_ids
from projectos.services.context import ServiceContext
from projectos.work_item_bootstrap import bootstrap_plan_work_items


def _bootstrap_repo(tmp_path: Path, project_id: str = "PRJ-004") -> tuple[Path, Path]:
    from projectos.paths import PROJECTOS_ROOT

    projects_root = tmp_path / "projects-root"
    template_root = PROJECTOS_ROOT / "templates" / "delivery-project"
    repo = bootstrap_project_repository(
        projects_root=projects_root,
        template_root=template_root,
        project_human_id=project_id,
        project_name="Calculator",
        raw_request="Build a calculator CLI",
    )
    python_executable = repo / ".venv" / "Scripts" / "python.exe"
    validate_project_control_state(
        repo, project_human_id=project_id, python_executable=python_executable
    )
    return repo, python_executable


def test_bootstrap_creates_story_before_plan_validation(tmp_path: Path) -> None:
    repo, python_executable = _bootstrap_repo(tmp_path)
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-004", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)

    plan = {
        "schema_version": 1,
        "project_human_id": "PRJ-004",
        "sponsor_authority": "approved",
        "jobs": [
            {
                "human_id": "PRJ-004-DEL-001",
                "queue": "DELIVERY",
                "agent_role": "DELIVERY",
                "work_item_type": "story",
                "work_item_human_id": "US-001",
                "title": "Calculator CLI core",
                "acceptance_criteria": ["Supports four operations"],
                "depends_on": [],
            }
        ],
    }
    result = run_plan(
        project_human_id="PRJ-004",
        dry_run=False,
        db_path=db,
        registry_path=tmp_path / "projects.json",
        plan_override=plan,
    )
    assert result.status == "accepted"
    assert "unknown work item" not in str(result.error or "")
    known = read_work_item_ids(repo, python_executable=python_executable)
    assert "US-001" in known.get("story", set())
    with connection(db) as conn:
        job = conn.execute(
            "SELECT work_item_human_id FROM orchestration_jobs WHERE human_id = ?",
            ("PRJ-004-DEL-001",),
        ).fetchone()
    assert job is not None
    assert job["work_item_human_id"] == "US-001"


def test_provisional_story_id_maps_to_authoritative_id(tmp_path: Path) -> None:
    repo, python_executable = _bootstrap_repo(tmp_path)
    seeded = create_projectctl_entity(
        repo,
        "story",
        title="Existing seed story",
        description="Pre-existing authoritative story",
        python_executable=python_executable,
    )
    seeded_id = None
    for line in (seeded.stdout or "").splitlines():
        if line.startswith("Created "):
            seeded_id = line.split()[1]
    assert seeded_id == "US-001"

    plan = {
        "schema_version": 1,
        "project_human_id": "PRJ-004",
        "sponsor_authority": "approved",
        "jobs": [
            {
                "human_id": "PRJ-004-DEL-001",
                "queue": "DELIVERY",
                "agent_role": "DELIVERY",
                "work_item_type": "story",
                "provisional_work_item_ref": "PROV-CALC-1",
                "work_item_human_id": "US-001",
                "title": "Calculator CLI core",
                "acceptance_criteria": ["Supports four operations"],
                "depends_on": [],
            }
        ],
    }
    known = read_work_item_ids(repo, python_executable=python_executable)
    bootstrap = bootstrap_plan_work_items(
        plan,
        repository_root=repo,
        python_executable=python_executable,
        known_work_items=known,
    )
    assert bootstrap.plan["jobs"][0]["work_item_human_id"] == "US-002"
    assert bootstrap.id_map["story:PROV-CALC-1"] == "US-002"
    assert bootstrap.id_map["story:PROV-CALC-1"] != "US-001"


def test_partial_bootstrap_failure_does_not_leave_orphan_jobs(
    tmp_path: Path, monkeypatch
) -> None:
    repo, python_executable = _bootstrap_repo(tmp_path)
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-004", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)

    calls = {"count": 0}

    def flaky_create(**kwargs):
        calls["count"] += 1
        if calls["count"] > 1:
            raise OrchestrationError("simulated persistence failure")
        from projectos.work_item_bootstrap import _create_work_item

        return _create_work_item(**kwargs)

    monkeypatch.setattr("projectos.work_item_bootstrap._create_work_item", flaky_create)

    plan = {
        "schema_version": 1,
        "project_human_id": "PRJ-004",
        "sponsor_authority": "approved",
        "jobs": [
            {
                "human_id": "JOB-A",
                "queue": "DELIVERY",
                "agent_role": "DELIVERY",
                "work_item_type": "story",
                "work_item_human_id": "US-010",
                "title": "Story A",
                "depends_on": [],
            },
            {
                "human_id": "JOB-B",
                "queue": "DELIVERY",
                "agent_role": "DELIVERY",
                "work_item_type": "story",
                "work_item_human_id": "US-011",
                "title": "Story B",
                "depends_on": [],
            },
        ],
    }

    with pytest.raises(OrchestrationError, match="simulated persistence failure"):
        run_plan(
            project_human_id="PRJ-004",
            dry_run=False,
            db_path=db,
            registry_path=tmp_path / "projects.json",
            plan_override=plan,
        )

    with connection(db) as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM orchestration_jobs").fetchone()
    assert int(total["total"]) == 0
