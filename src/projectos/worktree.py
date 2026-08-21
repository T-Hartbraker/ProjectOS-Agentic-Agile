"""Git worktree management for ProjectOS worker jobs."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from projectos.errors import GitRepositoryError, WorktreeError
from projectos.gitutil import resolve_git_root

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class WorktreeInfo:
    name: str
    path: Path
    repository_root: Path
    base_sha: str
    branch: str


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
    return completed.stdout


def current_head_sha(repository_root: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], cwd=repository_root).strip()


def is_dirty(worktree_path: Path) -> bool:
    out = _run_git(["status", "--porcelain"], cwd=worktree_path)
    return bool(out.strip())


def commit_all_changes(worktree_path: Path, message: str) -> str:
    """Stage and commit all changes in worktree; return new HEAD SHA.

    No-op (returns current HEAD) when the tree is clean.
    """
    if not is_dirty(worktree_path):
        return current_head_sha(worktree_path)
    _run_git(["add", "-A"], cwd=worktree_path)
    # Allow empty? No — if still nothing staged, treat as clean.
    staged = _run_git(["diff", "--cached", "--name-only"], cwd=worktree_path)
    if not staged.strip():
        return current_head_sha(worktree_path)
    _run_git(["commit", "-m", message], cwd=worktree_path)
    return current_head_sha(worktree_path)


def sha_belongs_to_repo(worktree_path: Path, sha: str) -> bool:
    try:
        _run_git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=worktree_path)
        return True
    except GitRepositoryError:
        return False


def common_git_dir(path: Path) -> Path:
    raw = _run_git(["rev-parse", "--git-common-dir"], cwd=path).strip()
    common = Path(raw)
    if not common.is_absolute():
        common = (path / common).resolve()
    else:
        common = common.resolve()
    return common


def list_worktrees(repository_root: Path) -> list[dict[str, str]]:
    """Parse `git worktree list --porcelain`."""
    text = _run_git(["worktree", "list", "--porcelain"], cwd=repository_root)
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {"worktree": line[len("worktree ") :]}
        elif line.startswith("HEAD "):
            current["HEAD"] = line[len("HEAD ") :]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :]
        elif line == "bare":
            current["bare"] = "1"
        elif line == "detached":
            current["detached"] = "1"
    if current:
        entries.append(current)
    return entries


def build_worktree_name(
    project_human_id: str,
    *,
    iteration_human_id: str | None,
    job_human_id: str,
) -> str:
    parts = [project_human_id]
    if iteration_human_id:
        parts.append(iteration_human_id)
    parts.append(job_human_id)
    name = "__".join(parts).replace("/", "-").replace("\\", "-").replace(" ", "_")
    if not _SAFE_NAME_RE.match(name):
        raise WorktreeError(f"Unsafe worktree name generated: {name!r}")
    return name


def default_worktree_path(repository_root: Path, name: str) -> Path:
    return (repository_root.parent / f"{repository_root.name}.worktrees" / name).resolve()


def ensure_worktree(
    repository_root: Path,
    *,
    name: str,
    path: Path | None = None,
    base_ref: str = "HEAD",
) -> WorktreeInfo:
    """Create or reuse a worktree under the registered repository."""
    if not _SAFE_NAME_RE.match(name):
        raise WorktreeError(f"Invalid worktree name: {name!r}")

    root = resolve_git_root(repository_root)
    if root != Path(repository_root).resolve():
        raise WorktreeError(
            f"repository_root {repository_root} is not the Git root {root}"
        )

    target = Path(path) if path is not None else default_worktree_path(root, name)
    root_common = common_git_dir(root)
    base_sha = _run_git(["rev-parse", base_ref], cwd=root).strip()
    branch = f"projectos/{name}"

    existing = {
        Path(e["worktree"]).resolve(): e
        for e in list_worktrees(root)
        if "worktree" in e
    }
    if target in existing:
        wt_common = common_git_dir(target)
        if wt_common != root_common:
            raise WorktreeError(
                f"Worktree {target} common dir {wt_common} does not match "
                f"registered repository {root_common}"
            )
        return WorktreeInfo(
            name=name,
            path=target,
            repository_root=root,
            base_sha=existing[target].get("HEAD", base_sha),
            branch=existing[target].get("branch", branch),
        )

    # Refuse if another path already uses this branch/name under this repo.
    for entry in existing.values():
        entry_path = Path(entry["worktree"]).resolve()
        if entry_path.name == name:
            raise WorktreeError(
                f"Worktree name {name!r} already claimed at {entry_path}"
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run_git(
            ["worktree", "add", "-b", branch, str(target), base_ref],
            cwd=root,
        )
    except GitRepositoryError:
        # Branch may already exist from a prior abandoned attempt.
        try:
            _run_git(
                ["worktree", "add", str(target), branch],
                cwd=root,
            )
        except GitRepositoryError as exc:
            raise WorktreeError(
                f"Failed to create worktree {name} at {target}: {exc}"
            ) from exc

    wt_common = common_git_dir(target)
    if wt_common != root_common:
        raise WorktreeError(
            f"Created worktree common dir {wt_common} does not match "
            f"registered repository {root_common}"
        )

    return WorktreeInfo(
        name=name,
        path=target.resolve(),
        repository_root=root,
        base_sha=current_head_sha(target),
        branch=branch,
    )
