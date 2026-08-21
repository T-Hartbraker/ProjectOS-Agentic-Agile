"""Helpers for orchestration worker tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.store import create_job


class FakeCompletedProcess:
    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "agent ok",
        stderr: str = "",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_cursor_runner(
    *,
    returncode: int = 0,
    stdout: str = "agent ok",
    stderr: str = "",
    timeout: bool = False,
    timeout_seconds: float = 1.0,
):
    def _runner(cmd, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(
                cmd=cmd,
                timeout=kwargs.get("timeout", timeout_seconds),
                output="partial",
                stderr="hanging",
            )
        return FakeCompletedProcess(returncode, stdout, stderr)

    return _runner


def write_registry(path: Path, projects: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "projects": projects}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def init_git_repo(repo_root: Path) -> Path:
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    marker = repo_root / ".gitkeep"
    marker.write_text("", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitkeep"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return repo_root.resolve()


def seed_db(db_path: Path) -> Path:
    initialize_database(db_path)
    return db_path


def add_ready_job(
    db_path: Path,
    *,
    human_id: str = "JOB-001",
    project_human_id: str = "PRJ-003",
    repository_root: Path,
    agent_role: str = "PM",
    queue: str = "PM",
    max_attempts: int = 3,
    attempt: int = 0,
    requires_worktree: bool = False,
    worktree_name: str | None = None,
    status: str = "READY",
) -> int:
    with connection(db_path) as conn:
        job = create_job(
            conn,
            human_id=human_id,
            project_human_id=project_human_id,
            repository_root=repository_root,
            agent_role=agent_role,
            queue=queue,
            status=status,
            max_attempts=max_attempts,
            attempt=attempt,
            requires_worktree=requires_worktree,
            worktree_name=worktree_name,
            identity_snapshot={
                "project_human_id": project_human_id,
                "repository_root": str(repository_root),
            },
        )
        return job.id
