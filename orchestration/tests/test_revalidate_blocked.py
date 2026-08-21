"""Governed BLOCKED job revalidation tests."""

from __future__ import annotations

import json
from pathlib import Path

from projectos.cli import main
from projectos.db import connection
from projectos.projectctl_bridge import ProjectctlStatusResult
from projectos.recover import revalidate_blocked_job
from projectos.store import (
    create_job,
    get_job_by_human_id,
    mark_blocked,
)
from projectos.worker import run_once

from orch_helpers import init_git_repo, make_cursor_runner, seed_db, write_registry


def _write_identity(repo: Path, project_human_id: str = "PRJ-003") -> None:
    d = repo / "project"
    d.mkdir(parents=True, exist_ok=True)
    (d / "repository.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository_type": "delivery-project",
                "project_human_id": project_human_id,
                "project_name": "Example",
                "isolation_model": "one-project-per-repository",
                "orchestration_scope": "project",
                "cross_project_access": False,
            }
        ),
        encoding="utf-8",
    )


def _cfg(tmp_path: Path, repo: Path) -> Path:
    _write_identity(repo)
    return write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-003",
                "repository_root": str(repo.resolve()),
                "enabled": True,
            }
        ],
    )


def _fake_status(human_id: str = "PRJ-003"):
    return lambda root: ProjectctlStatusResult(
        returncode=0,
        stdout=f"Active project: {human_id} - Example\n",
        stderr="",
        active_project_human_id=human_id,
        python_executable=Path("/fake/python"),
    )


def _show_story(*, title: str, description: str, human_id: str = "US-007"):
    def _fn(repository_root, work_item_type, work_item_human_id, **kwargs):
        assert work_item_type == "story"
        assert work_item_human_id == human_id
        return {
            "id": "7",
            "human_id": human_id,
            "title": title,
            "description": description,
            "status": "backlog",
        }

    return _fn


def test_recover_revalidate_cli_help() -> None:
    assert main(["recover", "--help"]) == 0


def test_missing_ac_blocks_delivery(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        create_job(
            conn,
            human_id="JOB-REWORK-AC",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            requires_worktree=True,
            work_item_type="story",
            work_item_human_id="US-007",
            base_git_sha="abc",
            assignment={
                "requirement_ref": "story:US-007",
                "title": "Due date",
                "acceptance_criteria": [],
            },
        )

    import projectos.prompt_builder as pb

    original = pb.show_work_item
    pb.show_work_item = _show_story(
        title="Due date",
        description="No criteria here",
    )
    try:
        result = run_once(
            db_path=db,
            registry_path=cfg,
            job_human_id="JOB-REWORK-AC",
            cursor_runner=make_cursor_runner(),
            skip_identity_validation=True,
            projectctl_runner=_fake_status(),
        )
    finally:
        pb.show_work_item = original

    assert result.status == "blocked"
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-REWORK-AC")
        assert job.status == "BLOCKED"
        assert "acceptance criteria are empty" in (job.last_error or "")
        blocked_events = conn.execute(
            """
            SELECT COUNT(*) FROM run_events
            WHERE job_id = ? AND status = 'BLOCKED'
              AND message LIKE ?
            """,
            (job.id, "%acceptance criteria%"),
        ).fetchone()[0]
        assert blocked_events >= 1


def test_ac_added_revalidate_ready_preserves_history(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    base_sha = "56d580d2eca1a634a86990241d4da2958c3323ff"
    block_msg = (
        "DELIVERY job JOB-REWORK-1 resolved story US-007 but "
        "acceptance criteria are empty"
    )
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-REWORK-1",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            work_item_type="story",
            work_item_human_id="US-007",
            base_git_sha=base_sha,
            assignment={
                "requirement_ref": "story:US-007",
                "title": "Due date",
                "acceptance_criteria": [],
            },
        )
        mark_blocked(conn, job.id, error=block_msg)
        before_events = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE job_id = ?", (job.id,)
        ).fetchone()[0]
        before_jobs = conn.execute(
            "SELECT COUNT(*) FROM orchestration_jobs"
        ).fetchone()[0]

    result = revalidate_blocked_job(
        job_human_id="JOB-REWORK-1",
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        show_work_item_fn=_show_story(
            title="Due date",
            description=(
                "Story text\n\nAcceptance Criteria:\n"
                "- AC-DUE-001: optional due date.\n"
                "- AC-DUE-002: overdue indication.\n"
            ),
        ),
    )
    assert result.ok
    assert result.status == "READY"
    assert result.acceptance_criteria_count >= 2
    assert result.created_duplicate is False

    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-REWORK-1")
        assert job.status == "READY"
        assert job.last_error is None
        assert job.work_item_human_id == "US-007"
        assert job.work_item_type == "story"
        assert job.base_git_sha == base_sha
        assert job.repository_root == str(repo)
        after_events = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE job_id = ?", (job.id,)
        ).fetchone()[0]
        assert after_events > before_events
        # Historical BLOCKED evidence preserved
        blocked = conn.execute(
            """
            SELECT COUNT(*) FROM run_events
            WHERE job_id = ? AND event_type = 'job.blocked'
            """,
            (job.id,),
        ).fetchone()[0]
        assert blocked >= 1
        revalidated = conn.execute(
            """
            SELECT COUNT(*) FROM run_events
            WHERE job_id = ? AND event_type = 'job.revalidated_ready'
            """,
            (job.id,),
        ).fetchone()[0]
        assert revalidated == 1
        after_jobs = conn.execute(
            "SELECT COUNT(*) FROM orchestration_jobs"
        ).fetchone()[0]
        assert after_jobs == before_jobs
        assignment = json.loads(job.assignment_json or "{}")
        assert any("AC-DUE-001" in a for a in assignment.get("acceptance_criteria", []))


def test_unresolved_blocker_remains_blocked(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-STILL-BLOCKED",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            work_item_type="story",
            work_item_human_id="US-007",
            assignment={"requirement_ref": "story:US-007", "acceptance_criteria": []},
        )
        mark_blocked(
            conn,
            job.id,
            error=(
                "DELIVERY job JOB-STILL-BLOCKED resolved story US-007 but "
                "acceptance criteria are empty"
            ),
        )

    result = revalidate_blocked_job(
        job_human_id="JOB-STILL-BLOCKED",
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        show_work_item_fn=_show_story(
            title="Due date",
            description="Still no acceptance criteria markers",
        ),
    )
    assert not result.ok
    assert result.status == "BLOCKED"
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-STILL-BLOCKED")
        assert job.status == "BLOCKED"
        assert "acceptance criteria are empty" in (job.last_error or "")
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM run_events
                WHERE job_id = ? AND event_type = 'job.revalidate_failed'
                """,
                (job.id,),
            ).fetchone()[0]
            >= 1
        )


def test_wrong_work_item_cannot_substitute(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-NO-SUB",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            work_item_type="story",
            work_item_human_id="US-007",
        )
        mark_blocked(
            conn,
            job.id,
            error=(
                "DELIVERY job JOB-NO-SUB resolved story US-007 but "
                "acceptance criteria are empty"
            ),
        )

    calls: list[str] = []

    def tracking_show(repository_root, work_item_type, work_item_human_id, **kwargs):
        calls.append(str(work_item_human_id))
        assert work_item_human_id == "US-007"
        return {
            "human_id": "US-007",
            "title": "Due",
            "description": "- AC-DUE-001: ok\n",
        }

    result = revalidate_blocked_job(
        job_human_id="JOB-NO-SUB",
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        show_work_item_fn=tracking_show,
    )
    assert result.ok
    assert calls == ["US-007"]
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-NO-SUB")
        assert job.work_item_human_id == "US-007"
        assert get_job_by_human_id(conn, "JOB-NO-SUB-US-999") is None


def test_identity_drift_cannot_unblock(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    other = init_git_repo(tmp_path / "other")
    _write_identity(other, "PRJ-003")
    # Registry points at a different root than the job binding.
    cfg = write_registry(
        tmp_path / "projects.json",
        [
            {
                "project_human_id": "PRJ-003",
                "repository_root": str(other.resolve()),
                "enabled": True,
            }
        ],
    )
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-DRIFT",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            work_item_type="story",
            work_item_human_id="US-007",
        )
        mark_blocked(
            conn,
            job.id,
            error=(
                "DELIVERY job JOB-DRIFT resolved story US-007 but "
                "acceptance criteria are empty"
            ),
        )

    result = revalidate_blocked_job(
        job_human_id="JOB-DRIFT",
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        show_work_item_fn=_show_story(
            title="Due",
            description="- AC-DUE-001: present\n",
        ),
    )
    assert not result.ok
    assert result.status == "BLOCKED"
    assert "Identity drift" in result.message
    with connection(db) as conn:
        job = get_job_by_human_id(conn, "JOB-DRIFT")
        assert job.status == "BLOCKED"
        assert job.repository_root == str(repo)


def test_generic_blocked_not_revalidated(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    cfg = _cfg(tmp_path, repo)
    db = seed_db(tmp_path / "projectos.db")
    with connection(db) as conn:
        job = create_job(
            conn,
            human_id="JOB-GENERIC",
            project_human_id="PRJ-003",
            repository_root=repo,
            agent_role="DELIVERY",
            queue="DELIVERY",
            status="READY",
            work_item_type="story",
            work_item_human_id="US-007",
        )
        mark_blocked(conn, job.id, error="manual operator hold")

    result = revalidate_blocked_job(
        job_human_id="JOB-GENERIC",
        db_path=db,
        registry_path=cfg,
        projectctl_runner=_fake_status(),
        show_work_item_fn=_show_story(
            title="Due",
            description="- AC-001: yes\n",
        ),
    )
    assert result.status == "BLOCKED"
    assert "Refusing" in result.message


def test_show_work_item_keeps_ac_lines_in_description() -> None:
    from projectos.projectctl_bridge import show_work_item
    import projectos.projectctl_bridge as bridge
    from projectos.prompt_builder import _acs_from_description

    class FakeResult:
        returncode = 0
        stdout = (
            "id: 7\n"
            "human_id: US-007\n"
            "title: Due\n"
            "description: Intro\n"
            "\n"
            "Acceptance Criteria:\n"
            "- AC-DUE-001: A task may be created with an optional due date.\n"
            "- AC-DUE-002: Overdue tasks are indicated.\n"
            "status: backlog\n"
        )
        stderr = ""

    original = bridge.run_projectctl
    bridge.run_projectctl = lambda *a, **k: FakeResult()  # type: ignore[assignment]
    try:
        parsed = show_work_item(Path("."), "story", "US-007")
    finally:
        bridge.run_projectctl = original
    assert parsed is not None
    assert "AC-DUE-001" in parsed["description"]
    assert len(_acs_from_description(parsed["description"])) == 2
