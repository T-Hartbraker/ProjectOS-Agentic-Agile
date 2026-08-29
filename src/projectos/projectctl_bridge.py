"""Repository-scoped projectctl adapter (never opens project.db directly)."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from projectos.errors import ProjectctlError, RegistryError
from projectos.registry import load_registry

_ACTIVE_PROJECT_RE = re.compile(
    r"^Active project:\s+(\S+)\s*-",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ProjectctlResult:
    returncode: int
    stdout: str
    stderr: str
    started_at: str
    ended_at: str
    duration_ms: int
    python_executable: Path
    command: tuple[str, ...]


@dataclass(frozen=True)
class ProjectctlStatusResult:
    returncode: int
    stdout: str
    stderr: str
    active_project_human_id: str | None
    python_executable: Path


def find_repository_python(
    repository_root: Path,
    *,
    explicit_python: Path | None = None,
) -> Path:
    if explicit_python is not None:
        path = Path(explicit_python).resolve()
        if not path.is_file():
            raise ProjectctlError(f"Configured Python not found: {path}")
        return path
    root = Path(repository_root)
    candidates = [
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        root / ".venv" / "bin" / "python3",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ProjectctlError(
        f"Repository Python environment not found under {root / '.venv'}. "
        "ProjectOS requires the delivery repository's own virtualenv to invoke "
        "projectctl."
    )


def parse_active_project_human_id(status_stdout: str) -> str | None:
    match = _ACTIVE_PROJECT_RE.search(status_stdout)
    if match:
        return match.group(1)
    if "No active project" in status_stdout:
        return None
    return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def run_projectctl(
    repository_root: Path,
    args: Sequence[str],
    *,
    python_executable: Path | None = None,
    timeout_seconds: float = 120.0,
    require_zero: bool = False,
    db_path: Path | None = None,
) -> ProjectctlResult:
    root = Path(repository_root).resolve()
    python = (
        Path(python_executable).resolve()
        if python_executable is not None
        else find_repository_python(root)
    )
    prefix = ["--repo-root", str(root)]
    if db_path is not None:
        prefix.extend(["--db", str(Path(db_path).resolve())])
    cmd = [str(python), "-m", "projectctl", *prefix, *list(args)]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    started_at = _iso_now()
    t0 = time.perf_counter()
    try:
        completed = subprocess.run(
            cmd,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ProjectctlError(f"Failed to execute repository Python at {python}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProjectctlError(
            f"projectctl timed out after {timeout_seconds}s in {root}"
        ) from exc
    ended_at = _iso_now()
    result = ProjectctlResult(
        returncode=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=int((time.perf_counter() - t0) * 1000),
        python_executable=python,
        command=tuple(cmd),
    )
    if require_zero and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ProjectctlError(
            f"projectctl {' '.join(args)} failed (exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return result


def run_projectctl_status(
    repository_root: Path,
    *,
    python_executable: Path | None = None,
    timeout_seconds: float = 60.0,
) -> ProjectctlStatusResult:
    result = run_projectctl(
        repository_root,
        ["status"],
        python_executable=python_executable,
        timeout_seconds=timeout_seconds,
    )
    return ProjectctlStatusResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        active_project_human_id=parse_active_project_human_id(result.stdout),
        python_executable=result.python_executable,
    )


def ensure_single_active_project(
    status: ProjectctlStatusResult,
    *,
    expected_human_id: str,
) -> str:
    if status.returncode != 0:
        detail = (status.stderr or status.stdout or "").strip()
        raise ProjectctlError(
            f"projectctl status failed (exit {status.returncode})"
            + (f": {detail}" if detail else "")
        )
    active = status.active_project_human_id
    if active is None:
        raise ProjectctlError(
            "projectctl status did not report exactly one active project "
            f"(expected {expected_human_id})"
        )
    if active != expected_human_id:
        raise ProjectctlError(
            f"projectctl active project {active} does not match expected "
            f"{expected_human_id}. Refusing to continue; never auto-repair."
        )
    return active


def resolve_validated_repo(
    project_human_id: str,
    *,
    registry_path: Path | str | None = None,
    projectctl_runner=None,
    claimed_repository_root: Path | str | None = None,
):
    from projectos.project_context import resolve_project_context

    return resolve_project_context(
        project_human_id,
        registry_path=registry_path,
        claimed_repository_root=claimed_repository_root,
        projectctl_runner=projectctl_runner,
    ).to_validated_project()


def projectctl_for_project(
    project_human_id: str,
    args: Sequence[str],
    *,
    registry_path: Path | str | None = None,
    mutating: bool = False,
    projectctl_runner=None,
    timeout_seconds: float = 120.0,
) -> ProjectctlResult:
    """Resolve via registry, optionally re-validate, then run projectctl args."""
    from projectos.validation import validate_registry_entry

    validated = resolve_validated_repo(
        project_human_id,
        registry_path=registry_path,
        projectctl_runner=projectctl_runner,
    )
    if mutating:
        validate_registry_entry(
            validated.entry, projectctl_runner=projectctl_runner or run_projectctl_status
        )
    return run_projectctl(
        validated.git_root,
        args,
        python_executable=validated.projectctl_python,
        timeout_seconds=timeout_seconds,
    )


def list_entity_ids(stdout: str) -> list[str]:
    """Parse first-column human IDs from projectctl list tables."""
    ids: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("human_id") or set(line) <= {"-", "="}:
            continue
        parts = line.split()
        if parts:
            ids.append(parts[0])
    return ids


def read_work_item_ids(
    repository_root: Path,
    *,
    python_executable: Path | None = None,
) -> dict[str, set[str]]:
    """Read known work-item IDs from projectctl lists (best-effort)."""
    mapping: dict[str, set[str]] = {
        "requirement": set(),
        "story": set(),
        "defect": set(),
        "iteration": set(),
        "release": set(),
    }
    for entity in mapping:
        try:
            result = run_projectctl(
                repository_root,
                [entity, "list"],
                python_executable=python_executable,
            )
            if result.returncode == 0:
                mapping[entity] = set(list_entity_ids(result.stdout))
        except ProjectctlError:
            continue
    return mapping


def create_defect(
    repository_root: Path,
    *,
    title: str,
    description: str | None = None,
    severity: str = "medium",
    python_executable: Path | None = None,
) -> ProjectctlResult:
    args = ["defect", "create", "--title", title, "--severity", severity]
    if description:
        args.extend(["--description", description])
    return run_projectctl(
        repository_root, args, python_executable=python_executable, require_zero=True
    )


def show_work_item(
    repository_root: Path,
    work_item_type: str,
    work_item_human_id: str,
    *,
    python_executable: Path | None = None,
) -> dict[str, str] | None:
    """Parse `projectctl <type> show <id>` key: value output."""
    entity = str(work_item_type).strip().lower()
    if entity not in {
        "requirement",
        "story",
        "defect",
        "iteration",
        "release",
        "risk",
        "assumption",
        "decision",
    }:
        return None
    try:
        result = run_projectctl(
            repository_root,
            [entity, "show", work_item_human_id],
            python_executable=python_executable,
        )
    except ProjectctlError:
        return None
    if result.returncode != 0:
        return None
    # Only treat identifier keys as field starts so AC lines like
    # "- AC-001: ..." remain part of description.
    field_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$")
    parsed: dict[str, str] = {}
    current_key: str | None = None
    for line in (result.stdout or "").splitlines():
        match = field_re.match(line)
        if match:
            current_key = match.group(1)
            parsed[current_key] = match.group(2).strip()
        elif current_key and line.strip():
            parsed[current_key] = (
                parsed.get(current_key, "") + "\n" + line.rstrip()
            ).strip()
    return parsed or None


def create_projectctl_entity(
    repository_root: Path,
    entity: str,
    *,
    title: str,
    description: str | None = None,
    python_executable: Path | None = None,
) -> ProjectctlResult:
    args = [entity, "create", "--title", title]
    if description:
        args.extend(["--description", description])
    return run_projectctl(
        repository_root, args, python_executable=python_executable, require_zero=True
    )


def ensure_iteration(
    repository_root: Path,
    iteration_human_id: str,
    *,
    name: str,
    python_executable: Path | None = None,
) -> str:
    """Ensure an iteration exists; create if missing. Returns human id."""
    known = read_work_item_ids(
        repository_root, python_executable=python_executable
    )
    if iteration_human_id in known.get("iteration", set()):
        return iteration_human_id
    # projectctl iteration create --name ...
    result = run_projectctl(
        repository_root,
        ["iteration", "create", "--name", name],
        python_executable=python_executable,
        require_zero=True,
    )
    for line in (result.stdout or "").splitlines():
        if line.startswith("Created "):
            return line.split()[1]
    refreshed = read_work_item_ids(
        repository_root, python_executable=python_executable
    )
    created = refreshed.get("iteration", set()) - known.get("iteration", set())
    if len(created) == 1:
        return next(iter(created))
    # Fall back: if requested id appeared somehow
    if iteration_human_id in refreshed.get("iteration", set()):
        return iteration_human_id
    raise ProjectctlError(
        f"Failed to ensure iteration {iteration_human_id}: {result.stdout}"
    )
