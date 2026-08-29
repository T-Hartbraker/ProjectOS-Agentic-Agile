"""Shared helpers for ProjectOS tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from projectos.paths import PROJECTS_SCHEMA_PATH
from projectos.projectctl_bridge import ProjectctlStatusResult


def write_registry(
    path: Path,
    projects: list[dict[str, Any]],
    *,
    schema_version: int = 1,
    corrupt: bool = False,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt:
        path.write_text("{not-json", encoding="utf-8")
        return path
    document = {"schema_version": schema_version, "projects": projects}
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def write_identity(
    repo_root: Path,
    *,
    project_human_id: str | None = "PRJ-003",
    project_name: str | None = "Example",
    repository_type: str = "delivery-project",
    corrupt: bool = False,
    omit_keys: tuple[str, ...] = (),
    overrides: dict[str, Any] | None = None,
) -> Path:
    project_dir = repo_root / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / "repository.json"
    if corrupt:
        path.write_text("{not-json", encoding="utf-8")
        return path
    data: dict[str, Any] = {
        "schema_version": 1,
        "repository_type": repository_type,
        "project_human_id": project_human_id,
        "project_name": project_name,
        "isolation_model": "one-project-per-repository",
        "orchestration_scope": "project",
        "cross_project_access": False,
    }
    if overrides:
        data.update(overrides)
    for key in omit_keys:
        data.pop(key, None)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def init_git_repo(repo_root: Path) -> Path:
    repo_root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    init = subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env=env,
    )
    if init.returncode != 0:
        subprocess.run(
            ["git", "init"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        subprocess.run(
            ["git", "checkout", "-B", "main"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    marker = repo_root / ".gitkeep"
    marker.write_text(f"init-{repo_root.name}\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitkeep"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    commit_cmd = [
        "git",
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-m",
        "init",
    ]
    for attempt in range(3):
        result = subprocess.run(
            commit_cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode == 0:
            break
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if "nothing to commit" in combined or "nothing added to commit" in combined:
            return repo_root.resolve()
        if attempt == 2:
            raise subprocess.CalledProcessError(
                result.returncode, commit_cmd, result.stdout, result.stderr
            )
        import time

        time.sleep(0.05 * (attempt + 1))
    return repo_root.resolve()


def fake_status(
    human_id: str,
    *,
    returncode: int = 0,
    python_executable: Path | None = None,
) -> ProjectctlStatusResult:
    stdout = f"Active project: {human_id} - Example\nStatus: active\n"
    return ProjectctlStatusResult(
        returncode=returncode,
        stdout=stdout if returncode == 0 else "",
        stderr="" if returncode == 0 else "error: isolation failure",
        active_project_human_id=human_id if returncode == 0 else None,
        python_executable=python_executable or Path("/fake/python"),
    )


def schema_path() -> Path:
    return PROJECTS_SCHEMA_PATH
