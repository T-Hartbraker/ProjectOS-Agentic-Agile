"""Deterministic Slack Sponsor intent classification before project resolution."""

from __future__ import annotations

import re
from enum import Enum

_PRJ_RE = re.compile(r"\b(PRJ-[A-Za-z0-9][A-Za-z0-9._-]*)\b", re.IGNORECASE)

_NEW_PROJECT_MARKERS = (
    "start a new project",
    "create a new project",
    "new project to",
    "new project for",
    "set up a new project",
    "stand up a new project",
    "new projectos project",
    "set up a new projectos project",
    "start another project",
    "let's start another project",
    "lets start another project",
    "i want to build a new app",
    "want to build a new app",
)

_OPERATOR_COMMANDS = frozenset(
    {
        "status",
        "summary",
        "help",
        "projects",
        "work",
        "quality",
        "qa",
        "releases",
        "release",
        "iteration",
        "blockers",
        "reports",
        "learning",
        "feedback",
        "defect",
        "use",
    }
)


class SlackIntent(Enum):
    NEW_PROJECT = "NEW_PROJECT"
    EXISTING_PROJECT_COMMAND = "EXISTING_PROJECT_COMMAND"
    EXISTING_PROJECT_WORK = "EXISTING_PROJECT_WORK"
    UNKNOWN = "UNKNOWN"


def _explicit_project_id(text: str) -> str | None:
    match = _PRJ_RE.search(str(text or ""))
    if not match:
        return None
    return match.group(1).upper()


def _has_new_project_marker(lowered: str) -> bool:
    return any(marker in lowered for marker in _NEW_PROJECT_MARKERS)


def _new_project_supersedes_existing_reference(lowered: str, project_id: str) -> bool:
    pid = project_id.casefold()
    if f"based on {pid}" in lowered:
        return True
    if f"lessons from {pid}" in lowered:
        return True
    if "start a new project" in lowered or "create a new project" in lowered:
        return True
    return False


def _existing_scope_on_project(lowered: str, project_id: str) -> bool:
    pid = project_id.casefold()
    if _new_project_supersedes_existing_reference(lowered, project_id):
        return False
    scoped_markers = (
        f"to {pid}",
        f"for {pid}",
        f"on {pid}",
        f"in {pid}",
        f"into {pid}",
        "new release for",
        "new feature to",
        "new screen to",
        "add a new",
        "create a new release",
    )
    if pid not in lowered:
        return False
    return any(marker in lowered for marker in scoped_markers)


def _is_operator_command(lowered: str) -> bool:
    tokens = lowered.split()
    if not tokens:
        return True
    first = tokens[0].lstrip("/")
    if first in _OPERATOR_COMMANDS:
        return True
    if len(tokens) >= 2 and tokens[0] in _OPERATOR_COMMANDS:
        return True
    if _PRJ_RE.match(tokens[0]) and len(tokens) >= 2 and tokens[1] in _OPERATOR_COMMANDS:
        return True
    if "status of" in lowered and _PRJ_RE.search(lowered):
        return True
    return False


def classify_projectos_intent(text: str) -> SlackIntent:
    """Classify Sponsor text before existing-project resolution."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return SlackIntent.UNKNOWN

    lowered = cleaned.casefold()
    project_id = _explicit_project_id(cleaned)
    new_project = _has_new_project_marker(lowered)

    if new_project:
        if project_id and _existing_scope_on_project(lowered, project_id):
            return SlackIntent.EXISTING_PROJECT_WORK
        return SlackIntent.NEW_PROJECT

    if project_id:
        if _is_operator_command(lowered):
            return SlackIntent.EXISTING_PROJECT_COMMAND
        return SlackIntent.EXISTING_PROJECT_WORK

    if _is_operator_command(lowered):
        return SlackIntent.EXISTING_PROJECT_COMMAND

    return SlackIntent.UNKNOWN
