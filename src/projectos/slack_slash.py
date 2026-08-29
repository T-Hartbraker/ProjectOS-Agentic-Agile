"""Translate Slack slash-command and conversational text into ProjectOS commands."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Any
from urllib.parse import parse_qs

from projectos.errors import OrchestrationError
from projectos.slack_commands import COMMANDS, INTAKE_COMMANDS
from projectos.slack_tokens import signing_secret as signing_secret_value
from projectos.store import require_safe_id

SLASH_NOTICE = (
    "Slack talks to this PC through Socket Mode. This machine must be running ProjectOS. "
    "Slack cannot grant Sponsor approval."
)

SOCKET_COMMANDS = frozenset(
    {
        "help",
        "status",
        "summary",
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
        "projects",
    }
)

_MENTION_RE = re.compile(r"<@[^>]+>")


def parse_form(raw: bytes) -> dict[str, str]:
    parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    return {key: (values[-1] if values else "") for key, values in parsed.items()}


def _looks_like_project_id(value: str) -> bool:
    text = str(value or "").strip()
    if not text.upper().startswith("PRJ-"):
        return False
    try:
        require_safe_id(text, label="project_human_id")
    except OrchestrationError:
        return False
    return True


def parse_slash_text(text: str) -> dict[str, str | None]:
    tokens = str(text or "").strip().split()
    if not tokens:
        return {"command": "status", "project_human_id": None, "title": None}
    if tokens[0].lower() == "use":
        project = tokens[1] if len(tokens) > 1 else None
        return {"command": "use", "project_human_id": project, "title": None}
    raw_command = tokens[0].lstrip("/")
    command = raw_command.lower()
    rest = tokens[1:]
    project = None
    if _looks_like_project_id(raw_command):
        project = raw_command
        if not rest:
            return {"command": "status", "project_human_id": project, "title": None}
        command = rest[0].lower()
        rest = rest[1:]
    elif rest and _looks_like_project_id(rest[0]):
        project = rest[0]
        rest = rest[1:]
    title = " ".join(rest).strip() or None
    return {"command": command, "project_human_id": project, "title": title}


def parse_conversational_text(text: str) -> dict[str, str | None]:
    cleaned = _MENTION_RE.sub("", str(text or "")).strip()
    if _looks_like_project_id(cleaned):
        return {"command": "use", "project_human_id": cleaned, "title": None}
    return parse_slash_text(cleaned)


def project_override_attempt(text: str) -> bool:
    """True when slash text uses deprecated flag-style project overrides."""
    tokens = str(text or "").replace("=", " ").split()
    flags = {"--project", "--proj"}
    for index, token in enumerate(tokens):
        lowered = token.lower()
        if lowered in flags or lowered.startswith("--project"):
            return True
        if lowered == "project" and index + 1 < len(tokens):
            return True
    return False


def verify_slack_request(
    *,
    signing_secret: str | None,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
) -> bool:
    secret = str(signing_secret or signing_secret_value() or "").strip()
    if not secret:
        return True
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > 60 * 5:
        return False
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    expected = "v0=" + digest
    return hmac.compare_digest(expected, signature.strip())


def slack_message(text: str, *, ephemeral: bool = False) -> dict[str, Any]:
    return {
        "response_type": "ephemeral" if ephemeral else "in_channel",
        "text": text,
    }


def handle_slash_form(form: dict[str, str], *, command_fn) -> dict[str, Any]:
    text = form.get("text") or ""
    if project_override_attempt(text):
        return slack_message(
            "Use `/projectos use PRJ-003` or `/projectos PRJ-003 status` to select a project.",
            ephemeral=True,
        )
    parsed = parse_slash_text(text)
    command = str(parsed["command"] or "status")
    if command not in COMMANDS and command not in {"use", "projects", "help", "summary", "work", "quality", "releases", "release", "qa", "status"}:
        allowed = ", ".join(sorted(COMMANDS | {"use", "projects", "help", "summary", "work", "quality", "releases"}))
        return slack_message(
            f"Unknown ProjectOS command {command!r}. Try: {allowed}",
            ephemeral=True,
        )
    title = parsed["title"]
    if command in INTAKE_COMMANDS and not title:
        return slack_message(
            f"{command} needs a title. Example: /projectos {command} Checkout hangs on save",
            ephemeral=True,
        )
    try:
        result = command_fn(
            command=command,
            channel_id=str(form.get("channel_id") or "").strip(),
            team_id=form.get("team_id") or None,
            thread_ts=None,
            message_ts=form.get("trigger_id") or None,
            project_human_id=parsed.get("project_human_id"),
            title=title,
            description=title,
            source="slack",
        )
    except OrchestrationError as exc:
        return slack_message(str(exc), ephemeral=True)
    text = str(result.get("text") or "").strip() or "No summary."
    notice = str(result.get("notice") or SLASH_NOTICE)
    return slack_message(f"{text}\n\n{notice}")
