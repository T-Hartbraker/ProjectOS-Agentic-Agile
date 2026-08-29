"""Repository hygiene — reject machine-local artifacts and forbidden paths in Git."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_TRACKED_PATTERNS = (
    re.compile(r"^state/.+\.db$"),
    re.compile(r"^state/openai_state\.json$"),
    re.compile(r"^config/projects\.json$"),
    re.compile(r"^config/operator\.json$"),
)

FORBIDDEN_PATH_CONTENT = re.compile(
    r"C:\\Dev\\|C:/Dev/|PersonalTaskManager",
    re.IGNORECASE,
)

ALLOWED_PATH_PREFIXES = (
    "docs/",
    "orchestration/tests/",
)


def _git_tracked_files() -> list[str]:
    import subprocess

    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_no_machine_local_runtime_files_tracked() -> None:
    tracked = _git_tracked_files()
    violations = []
    for path in tracked:
        for pattern in FORBIDDEN_TRACKED_PATTERNS:
            if pattern.match(path.replace("\\", "/")):
                violations.append(path)
    assert not violations, f"Machine-local runtime files must not be tracked: {violations}"


def test_no_forbidden_absolute_paths_in_product_source() -> None:
    violations: list[str] = []
    for path in _git_tracked_files():
        normalized = path.replace("\\", "/")
        if not normalized.startswith("src/"):
            continue
        if any(normalized.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES):
            continue
        content = (REPO_ROOT / path).read_text(encoding="utf-8", errors="ignore")
        if FORBIDDEN_PATH_CONTENT.search(content):
            violations.append(path)
    assert not violations, f"Forbidden machine-specific paths in product source: {violations}"


def test_projects_example_template_exists() -> None:
    example = REPO_ROOT / "config" / "projects.example.json"
    assert example.is_file(), "config/projects.example.json must exist for onboarding"
