"""Git repository helpers for ProjectOS path boundaries."""

from __future__ import annotations

import subprocess
from pathlib import Path

from projectos.errors import GitRepositoryError, PathBoundaryError


def _run_git(args: list[str], *, cwd: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise GitRepositoryError("git executable not found") from exc
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise GitRepositoryError(err or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def resolve_git_root(repository_root: Path) -> Path:
    """Return the Git toplevel for repository_root, or raise if missing."""
    root = Path(repository_root).resolve()
    if not root.exists():
        raise GitRepositoryError(f"Repository root does not exist: {root}")
    if not root.is_dir():
        raise GitRepositoryError(f"Repository root is not a directory: {root}")
    try:
        toplevel = _run_git(["rev-parse", "--show-toplevel"], cwd=root)
    except GitRepositoryError as exc:
        raise GitRepositoryError(
            f"No Git repository found at or above {root}: {exc}"
        ) from exc
    return Path(toplevel).resolve()


def assert_within_git_root(path: Path, git_root: Path) -> Path:
    """Resolve path and reject if it falls outside the Git root.

    Never rewrites paths; callers must supply the intended location.
    """
    resolved = Path(path).resolve()
    root = Path(git_root).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathBoundaryError(
            f"Path {resolved} is outside repository Git root {root}"
        ) from exc
    return resolved
