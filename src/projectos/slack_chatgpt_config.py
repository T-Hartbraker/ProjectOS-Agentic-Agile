"""Slack ChatGPT Advisor trigger configuration (non-secret)."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from projectos.errors import OrchestrationError
from projectos.paths import STATE_DIR

DEFAULT_CHATGPT_SLACK_USER_ID = "U0BTHBJK51A"
CHATGPT_USER_ID_ENV = "PROJECTOS_SLACK_CHATGPT_USER_ID"
_CONFIG_PATH = STATE_DIR / "slack_chatgpt_config.json"
_SLACK_USER_ID_RE = re.compile(r"^U[A-Z0-9]{2,}$")


def _read_config() -> dict[str, str]:
    if not _CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_config(updates: dict[str, str]) -> None:
    current = _read_config()
    current.update(updates)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")


def validate_chatgpt_slack_user_id(value: str) -> str:
    text = str(value or "").strip().upper()
    if not _SLACK_USER_ID_RE.match(text):
        raise OrchestrationError("Slack user ID must look like U0123456789")
    return text


def stored_chatgpt_slack_user_id() -> str | None:
    value = str(_read_config().get("user_id") or "").strip().upper()
    return value or None


def chatgpt_slack_user_id() -> str:
    env = str(os.environ.get(CHATGPT_USER_ID_ENV) or "").strip().upper()
    if env:
        return env
    stored = stored_chatgpt_slack_user_id()
    if stored:
        return stored
    return DEFAULT_CHATGPT_SLACK_USER_ID


def chatgpt_slack_user_id_source() -> str:
    if str(os.environ.get(CHATGPT_USER_ID_ENV) or "").strip():
        return "environment"
    if stored_chatgpt_slack_user_id():
        return "settings"
    return "default"


def set_chatgpt_slack_user_id(value: str) -> str:
    validated = validate_chatgpt_slack_user_id(value)
    _write_config({"user_id": validated})
    return validated


def chatgpt_mention_pattern(user_id: str | None = None) -> re.Pattern[str]:
    uid = str(user_id or chatgpt_slack_user_id() or "").strip().upper()
    if not uid:
        return re.compile(r"(?!x)x")
    return re.compile(rf"<@{re.escape(uid)}(?:\|[^>]+)?>", re.IGNORECASE)


def strip_chatgpt_mention(text: str, *, event: dict[str, Any] | None = None) -> str:
    uid = chatgpt_slack_user_id()
    cleaned = chatgpt_mention_pattern(uid).sub("", str(text or ""), count=1).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned:
        return cleaned
    if event is not None:
        for block in event.get("blocks") or []:
            if isinstance(block, dict):
                for element in block.get("elements") or []:
                    if isinstance(element, dict) and element.get("type") == "text":
                        fallback = str(element.get("text") or "").strip()
                        if fallback:
                            return fallback
    return str(text or "").strip()
