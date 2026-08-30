"""Git revision helpers for release candidate gates."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)


@dataclass(frozen=True)
class GitSnapshot:
    """Observable git state used by release gates."""

    head_sha: str | None
    working_tree_clean: bool
    sha_exists: Callable[[str], bool]


class GitError(Exception):
    """Git inspection failure."""


def _run_git(args: list[str], *, cwd: Path | None) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found") from exc
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise GitError(err or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def normalize_git_sha(value: str) -> str:
    sha = value.strip().lower()
    if not _SHA_RE.match(sha):
        raise GitError(f"Invalid git SHA: {value!r}")
    return sha


def inspect_git(repo_root: Path | None = None) -> GitSnapshot:
    """Return HEAD, cleanliness, and an existence checker for the repo."""
    cwd = repo_root

    def sha_exists(sha: str) -> bool:
        try:
            _run_git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=cwd)
            return True
        except GitError:
            return False

    head = _run_git(["rev-parse", "HEAD"], cwd=cwd)
    status = _run_git(["status", "--porcelain"], cwd=cwd)
    return GitSnapshot(
        head_sha=head.lower(),
        working_tree_clean=status == "",
        sha_exists=sha_exists,
    )


def fake_git_snapshot(
    *,
    head_sha: str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    working_tree_clean: bool = True,
    known_shas: set[str] | None = None,
) -> GitSnapshot:
    """Test helper: deterministic git snapshot without invoking git."""
    known = {s.lower() for s in (known_shas or {head_sha})}
    known.add(head_sha.lower())
    return GitSnapshot(
        head_sha=head_sha.lower(),
        working_tree_clean=working_tree_clean,
        sha_exists=lambda s: s.lower() in known,
    )
