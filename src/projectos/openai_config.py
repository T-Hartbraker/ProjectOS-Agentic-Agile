"""Non-secret OpenAI configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path

from projectos.errors import OrchestrationError
from projectos.paths import STATE_DIR

DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
MODEL_ENV = "PROJECTOS_OPENAI_MODEL"
ENABLED_ENV = "PROJECTOS_OPENAI_ENABLED"
_CONFIG_PATH = STATE_DIR / "openai_config.json"

SUPPORTED_OPENAI_MODELS: tuple[str, ...] = (
    "gpt-4.1-mini",
    "gpt-4.1",
    "gpt-4o-mini",
    "gpt-4o",
)


def openai_enabled() -> bool:
    raw = str(os.environ.get(ENABLED_ENV) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


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


def stored_openai_model() -> str | None:
    value = str(_read_config().get("model") or "").strip()
    return value or None


def validate_openai_model(model: str) -> str:
    text = str(model or "").strip()
    if text not in SUPPORTED_OPENAI_MODELS:
        supported = ", ".join(SUPPORTED_OPENAI_MODELS)
        raise OrchestrationError(f"Unsupported OpenAI model. Choose one of: {supported}")
    return text


def set_openai_model(model: str) -> str:
    validated = validate_openai_model(model)
    _write_config({"model": validated})
    return validated


def openai_model() -> str:
    env = str(os.environ.get(MODEL_ENV) or "").strip()
    if env:
        return env
    stored = stored_openai_model()
    if stored:
        return stored
    return DEFAULT_OPENAI_MODEL


def openai_model_source() -> str:
    if str(os.environ.get(MODEL_ENV) or "").strip():
        return "environment"
    if stored_openai_model():
        return "settings"
    return "default"
