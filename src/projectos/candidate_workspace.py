"""Immutable candidate workspace for trusted release builds."""

from __future__ import annotations

import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from projectos.errors import OrchestrationError


def git_object_exists(repo_root: Path, git_sha: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{git_sha}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


@contextmanager
def candidate_workspace(
    repo_root: Path,
    candidate_sha: str,
    *,
    parent_dir: Path,
) -> Iterator[Path]:
    """Detached worktree at exact candidate SHA — never mutates main checkout."""
    if not candidate_sha:
        raise OrchestrationError("candidate_workspace requires candidate_sha")
    if not git_object_exists(repo_root, candidate_sha):
        raise OrchestrationError(f"Candidate {candidate_sha} not found in repository")

    workspace = parent_dir / f"candidate-{candidate_sha[:12]}"
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "worktree", "add", "--detach", str(workspace), candidate_sha],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OrchestrationError(
            f"Failed to create candidate workspace: {result.stderr.strip() or result.stdout}"
        )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if head != candidate_sha:
        raise OrchestrationError(
            f"Candidate workspace HEAD {head} != expected {candidate_sha}"
        )
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty:
        raise OrchestrationError("Candidate workspace is dirty after checkout")
    try:
        yield workspace
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(workspace)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
