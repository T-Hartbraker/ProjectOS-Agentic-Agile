"""FAT-01 regression: explicit new-project Sponsor intent bypasses existing-project resolution."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers import fake_status, init_git_repo, write_identity, write_registry
from projectos.cursor_adapter import CursorRunResult
from projectos.db import connection
from projectos.errors import ProjectctlError
from projectos.migrate import initialize_database
from projectos.project_context import resolve_project_context
from projectos.project_creation import (
    allocate_project_id,
    validate_project_control_state,
)
from projectos.projectctl_bridge import run_projectctl_status
from projectos.registry import load_registry
from projectos.repository import load_repository_identity
from projectos.services.context import ServiceContext
from projectos.slack_intent import SlackIntent, classify_projectos_intent
from projectos.projectctl_bridge import read_work_item_ids
from projectos.slack_socket import handle_events_api_payload, handle_projectos_request
from projectos.sponsor_handoff import get_latest_thread_handoff
from projectos.store import add_slack_interface_channel, get_job_by_human_id, get_slack_project_context

FAT_MESSAGE = (
    "Start a new project to build a simple Python command-line calculator. It must "
    "support addition, subtraction, multiplication, and division, include automated "
    "tests, and be packaged as a distributable ZIP. Use the full ProjectOS delivery "
    "process. Proceed autonomously through implementation, QA, remediation if "
    "required, packaging, and release. Ask me only if a Sponsor decision is genuinely "
    "required."
)


@pytest.fixture
def fast_plan_cursor(monkeypatch):
    def _fake_cursor(**kwargs):
        project_id = "PRJ-004"
        return CursorRunResult(
            command=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "schema_version": 1,
                    "project_human_id": project_id,
                    "iteration_human_id": "ITER-001",
                    "sponsor_authority": "approved",
                    "jobs": [
                        {
                            "human_id": f"{project_id}-ARCH-001",
                            "queue": "ARCHITECTURE",
                            "agent_role": "ARCHITECTURE",
                            "depends_on": [],
                        },
                        {
                            "human_id": f"{project_id}-DEL-001",
                            "queue": "DELIVERY",
                            "agent_role": "DELIVERY",
                            "work_item_type": "story",
                            "work_item_human_id": "US-001",
                            "title": "Calculator CLI with four operations",
                            "acceptance_criteria": [
                                "Supports addition, subtraction, multiplication, and division",
                                "Includes automated tests",
                            ],
                            "depends_on": [f"{project_id}-ARCH-001"],
                        },
                    ],
                }
            ),
            stderr="",
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=1,
            output_ref="test-plan",
            stdout_ref="test-plan-out",
            stderr_ref="test-plan-err",
            prompt_ref=None,
            workspace=kwargs.get("workspace", Path.cwd()),
            worktree_name=None,
            usage=None,
        )

    monkeypatch.setattr("projectos.plan.invoke_cursor_agent", _fake_cursor)


def _runner(human_id: str):
    return lambda root: fake_status(human_id)


def _write_defaults(tmp_path: Path, projects_root: Path) -> Path:
    from projectos.paths import PROJECTOS_ROOT

    path = tmp_path / "project_defaults.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects_root": str(projects_root.resolve()),
                "delivery_template_root": str(
                    (PROJECTOS_ROOT / "templates" / "delivery-project").resolve()
                ),
            }
        ),
        encoding="utf-8",
    )
    return path


def _ctx(tmp_path: Path) -> tuple[ServiceContext, Path]:
    projects_root = tmp_path / "projects-root"
    projects_root.mkdir(parents=True, exist_ok=True)
    repo = init_git_repo(tmp_path / "alpha")
    write_identity(repo, project_human_id="PRJ-003", project_name="Personal Task Manager Pilot")
    write_registry(
        tmp_path / "projects.json",
        [{"project_human_id": "PRJ-003", "repository_root": str(repo.resolve()), "enabled": True}],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    with connection(db) as conn:
        add_slack_interface_channel(conn, channel_id="G_PRIVATE", team_id="T1", is_default=True)
    defaults_path = _write_defaults(tmp_path, projects_root)
    ctx = ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")
    return ctx, defaults_path


def _mention_event(*, ts: str = "200.0", text: str, event_id: str = "EvFat01") -> dict:
    return {
        "event_id": event_id,
        "team_id": "T1",
        "event": {
            "type": "app_mention",
            "channel": "G_PRIVATE",
            "ts": ts,
            "user": "U1",
            "text": f"<@UBOT> {text}",
        },
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Start a new project to build a calculator.", SlackIntent.NEW_PROJECT),
        ("Create a new project for inventory management.", SlackIntent.NEW_PROJECT),
        ("Set up a new ProjectOS project for a CLI.", SlackIntent.NEW_PROJECT),
        ("Add a new feature to PRJ-003.", SlackIntent.EXISTING_PROJECT_WORK),
        ("Create a new release for PRJ-003.", SlackIntent.EXISTING_PROJECT_WORK),
        ("Start a new project based on PRJ-003.", SlackIntent.NEW_PROJECT),
        ("PRJ-003 status", SlackIntent.EXISTING_PROJECT_COMMAND),
    ],
)
def test_classify_projectos_intent_cases(text: str, expected: SlackIntent) -> None:
    assert classify_projectos_intent(text) == expected


def test_direct_projectos_new_project_request_creates_project_and_starts_run(
    tmp_path: Path, monkeypatch, fast_plan_cursor
) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx, defaults_path = _ctx(tmp_path)
    projects_root = json.loads(defaults_path.read_text(encoding="utf-8"))["projects_root"]
    monkeypatch.setenv("PROJECTOS_PROJECTS_ROOT", projects_root)

    reply = handle_events_api_payload(
        ctx,
        _mention_event(text=FAT_MESSAGE),
        bot_user_id="UBOT",
    )
    assert reply is not None
    assert "select a project" not in reply["text"].casefold()
    assert "/projectos use PRJ-003" not in reply["text"]
    assert "New project initiated" in reply["text"]
    assert "PRJ-004" in reply["text"]
    assert "RUN-" in reply["text"]

    registry = load_registry(ctx.registry_path)
    ids = {entry.project_human_id for entry in registry.projects}
    assert ids == {"PRJ-003", "PRJ-004"}

    new_entry = registry.get("PRJ-004")
    assert new_entry is not None
    repo = Path(new_entry.repository_root)
    assert repo.is_dir()
    assert (repo / ".git").exists()
    identity = load_repository_identity(repo)
    assert identity.project_human_id == "PRJ-004"
    assert (repo / "project" / "delivery.json").is_file()
    delivery = json.loads((repo / "project" / "delivery.json").read_text(encoding="utf-8"))
    assert delivery["installer_format"] == "zip"
    assert not (repo / "project-control" / ".projectctl-bootstrap").exists()

    db_path = repo / "project-control" / "project.db"
    assert db_path.stat().st_size > 0
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "projects" in tables
        active = conn.execute(
            "SELECT human_id FROM projects WHERE is_active = 1"
        ).fetchone()
        assert active is not None
        assert active[0] == "PRJ-004"

    venv_python = repo / ".venv" / "Scripts" / "python.exe"
    validate_project_control_state(
        repo, project_human_id="PRJ-004", python_executable=venv_python
    )
    status = run_projectctl_status(repo)
    assert "PRJ-004" in status.stdout

    project_ctx = resolve_project_context(
        "PRJ-004",
        registry_path=ctx.registry_path,
    )
    assert project_ctx.project_human_id == "PRJ-004"

    with connection(ctx.db_path) as conn:
        slack_ctx = get_slack_project_context(
            conn,
            team_id="T1",
            channel_id="G_PRIVATE",
            thread_ts="200.0",
            user_id="U1",
        )
        handoff = get_latest_thread_handoff(
            conn, team_id="T1", channel_id="G_PRIVATE", thread_ts="200.0"
        )
        run_row = conn.execute(
            "SELECT run_id, objective, status, result_summary FROM execution_runs WHERE project_id = ?",
            ("PRJ-004",),
        ).fetchone()
        decision_rows = conn.execute(
            "SELECT reason FROM governance_decisions WHERE project_human_id = ?",
            ("PRJ-004",),
        ).fetchall()
        next_actions = conn.execute(
            "SELECT action_type, status FROM run_next_actions WHERE run_id = ?",
            (run_row["run_id"],),
        ).fetchall()
        handoff_constraints = conn.execute(
            "SELECT constraints_json FROM sponsor_handoffs WHERE handoff_id = ?",
            (handoff.handoff_id,),
        ).fetchone()

    assert slack_ctx is not None
    assert slack_ctx["project_human_id"] == "PRJ-004"
    assert handoff is not None
    assert handoff.project_id == "PRJ-004"
    assert "calculator" in handoff.objective.casefold()
    assert run_row is not None
    assert "zip" in run_row["objective"].casefold()
    assert "calculator" in run_row["objective"].casefold()
    assert run_row["status"] == "RUNNING"
    assert run_row["status"] != "WAITING_FOR_SPONSOR"
    assert "Sponsor approval required before execution" not in str(
        run_row["result_summary"] or ""
    )
    constraints = json.loads(handoff_constraints["constraints_json"])
    assert constraints.get("execution_authorized") is True
    assert constraints.get("authority_source") == "explicit_new_project"
    assert constraints.get("authority_ingress") == "slack_new_project"
    assert constraints.get("sponsor_user_id") == "U1"
    assert len(next_actions) >= 1
    assert any(row["action_type"] == "EXECUTABLE_JOB" for row in next_actions)
    assert all(row["status"] == "pending" for row in next_actions)
    desired = json.loads(handoff.desired_outputs_json or "{}")
    assert desired.get("zip_package") is True
    assert not any("scope_new_venture" in str(row["reason"]).casefold() for row in decision_rows)

    known = read_work_item_ids(repo, python_executable=venv_python)
    assert "US-001" in known.get("story", set())
    with connection(ctx.db_path) as conn:
        delivery_job = get_job_by_human_id(conn, "PRJ-004-DEL-001")
        failure_events = conn.execute(
            """
            SELECT COUNT(*) AS total FROM projectos_events
            WHERE run_id = ? AND event_type = 'OPERATION_FAILED'
            """,
            (run_row["run_id"],),
        ).fetchone()
    assert delivery_job is not None
    assert delivery_job.work_item_human_id == "US-001"
    assert int(failure_events["total"]) == 0

    original = load_repository_identity(Path(registry.get("PRJ-003").repository_root))
    assert original.project_human_id == "PRJ-003"


def test_existing_project_status_regression_unchanged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx, defaults_path = _ctx(tmp_path)
    reply = handle_projectos_request(
        ctx,
        text="PRJ-003 status",
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="201.0",
        thread_root_ts="201.0",
        user_id="U1",
        project_defaults_path=defaults_path,
        projectctl_runner=_runner("PRJ-003"),
    )
    assert reply is not None
    assert "PRJ-003" in reply["text"]
    assert "New project initiated" not in reply["text"]
    registry = load_registry(ctx.registry_path)
    assert {entry.project_human_id for entry in registry.projects} == {"PRJ-003"}


def test_add_feature_to_existing_project_does_not_create_project(tmp_path: Path) -> None:
    ctx, defaults_path = _ctx(tmp_path)
    reply = handle_projectos_request(
        ctx,
        text="Add a new calculator operation to PRJ-003",
        channel_id="G_PRIVATE",
        team_id="T1",
        thread_ts="202.0",
        thread_root_ts="202.0",
        user_id="U1",
        project_defaults_path=defaults_path,
        projectctl_runner=_runner("PRJ-003"),
    )
    assert reply is not None
    assert "New project initiated" not in reply["text"]
    registry = load_registry(ctx.registry_path)
    assert {entry.project_human_id for entry in registry.projects} == {"PRJ-003"}


def test_duplicate_slack_event_is_idempotent(tmp_path: Path, monkeypatch, fast_plan_cursor) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx, defaults_path = _ctx(tmp_path)
    projects_root = json.loads(defaults_path.read_text(encoding="utf-8"))["projects_root"]
    monkeypatch.setenv("PROJECTOS_PROJECTS_ROOT", projects_root)

    payload = _mention_event(text=FAT_MESSAGE, event_id="EvDup", ts="203.0")
    first = handle_events_api_payload(ctx, payload, bot_user_id="UBOT")
    second = handle_events_api_payload(ctx, payload, bot_user_id="UBOT")
    assert first is not None
    assert second is None
    registry = load_registry(ctx.registry_path)
    assert len(registry.projects) == 2
    with connection(ctx.db_path) as conn:
        handoffs = conn.execute("SELECT COUNT(*) AS total FROM sponsor_handoffs").fetchone()
        runs = conn.execute("SELECT COUNT(*) AS total FROM execution_runs").fetchone()
    assert int(handoffs["total"]) == 1
    assert int(runs["total"]) == 1


def test_concurrent_project_id_allocation(tmp_path: Path) -> None:
    ctx, defaults_path = _ctx(tmp_path)
    initialize_database(ctx.db_path)
    results: list[str] = []
    errors: list[Exception] = []

    def _allocate() -> None:
        try:
            with connection(ctx.db_path) as conn:
                results.append(allocate_project_id(conn, registry_path=ctx.registry_path))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_allocate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(results) == 2
    assert len(set(results)) == 2


def test_allocator_skips_disabled_ids(tmp_path: Path) -> None:
    repo_a = init_git_repo(tmp_path / "a")
    repo_b = init_git_repo(tmp_path / "b")
    repo_c = init_git_repo(tmp_path / "c")
    write_identity(repo_a, project_human_id="PRJ-001")
    write_identity(repo_b, project_human_id="PRJ-010")
    write_identity(repo_c, project_human_id="PRJ-003")
    write_registry(
        tmp_path / "projects.json",
        [
            {"project_human_id": "PRJ-001", "repository_root": str(repo_a.resolve()), "enabled": True},
            {"project_human_id": "PRJ-003", "repository_root": str(repo_c.resolve()), "enabled": False},
            {"project_human_id": "PRJ-010", "repository_root": str(repo_b.resolve()), "enabled": True},
        ],
    )
    db = tmp_path / "projectos.db"
    initialize_database(db)
    ctx = ServiceContext(db_path=db, registry_path=tmp_path / "projects.json")
    with connection(ctx.db_path) as conn:
        allocated = allocate_project_id(conn, registry_path=ctx.registry_path)
    assert allocated == "PRJ-011"


def test_projectctl_init_failure_rolls_back_without_synthetic_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PROJECTOS_SLACK_BOT_TOKEN", "xoxb-test")
    ctx, defaults_path = _ctx(tmp_path)
    projects_root = Path(
        json.loads(defaults_path.read_text(encoding="utf-8"))["projects_root"]
    )
    monkeypatch.setenv("PROJECTOS_PROJECTS_ROOT", str(projects_root))

    def _fail_projectctl(*args, **kwargs):
        raise ProjectctlError("simulated projectctl initialization failure")

    monkeypatch.setattr("projectos.project_creation._initialize_projectctl", _fail_projectctl)

    reply = handle_events_api_payload(
        ctx,
        _mention_event(text=FAT_MESSAGE, event_id="EvFail", ts="204.0"),
        bot_user_id="UBOT",
    )
    assert reply is not None
    assert "projectctl initialization" in reply["text"].casefold()

    registry = load_registry(ctx.registry_path)
    assert {entry.project_human_id for entry in registry.projects} == {"PRJ-003"}
    remaining = [path for path in projects_root.glob("*") if path.is_dir()]
    assert remaining == []

    with connection(ctx.db_path) as conn:
        handoffs = conn.execute("SELECT COUNT(*) AS total FROM sponsor_handoffs").fetchone()
        runs = conn.execute("SELECT COUNT(*) AS total FROM execution_runs").fetchone()
        reservations = conn.execute(
            "SELECT COUNT(*) AS total FROM project_id_reservations"
        ).fetchone()
    assert int(handoffs["total"]) == 0
    assert int(runs["total"]) == 0
    assert int(reservations["total"]) == 0
