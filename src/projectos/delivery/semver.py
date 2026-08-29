"""Semantic version parsing and bump proposals."""

from __future__ import annotations

import re

from projectos.errors import OrchestrationError

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


def parse_semver(value: str) -> tuple[int, int, int, str | None]:
    text = str(value or "").strip().lstrip("vV")
    match = SEMVER_RE.match(text)
    if not match:
        raise OrchestrationError(f"Invalid semantic version: {value!r}")
    major, minor, patch, prerelease = match.groups()
    return int(major), int(minor), int(patch), prerelease


def format_semver(major: int, minor: int, patch: int, *, prerelease: str | None = None) -> str:
    base = f"{major}.{minor}.{patch}"
    if prerelease:
        return f"{base}-{prerelease}"
    return base


def format_tag(version: str) -> str:
    text = str(version or "").strip()
    return text if text.startswith("v") else f"v{text}"


def propose_bump(current: str, *, change_type: str) -> str:
    major, minor, patch, prerelease = parse_semver(current)
    if prerelease:
        raise OrchestrationError("Cannot auto-bump a prerelease version")
    kind = str(change_type or "").strip().lower()
    if kind in {"patch", "bugfix", "fix"}:
        return format_semver(major, minor, patch + 1)
    if kind in {"minor", "feature"}:
        return format_semver(major, minor + 1, 0)
    if kind in {"major", "breaking"}:
        return format_semver(major + 1, 0, 0)
    raise OrchestrationError(f"Unsupported version bump type: {change_type!r}")
