"""Read OpenAI credentials. Never persist or return secret values in APIs."""

from __future__ import annotations

import os
from typing import Any, Literal

from projectos.openai_config import openai_enabled, openai_model
from projectos.secret_store import read_openai_secrets

API_KEY_ENV = "PROJECTOS_OPENAI_API_KEY"
KEY_PREFIX = "sk-"

ApiKeySource = Literal["environment", "encrypted_store", "none"]

_vault_cache: dict[str, str] | None = None


def _vault() -> dict[str, str]:
    global _vault_cache
    if _vault_cache is None:
        _vault_cache = read_openai_secrets()
    return _vault_cache


def reload_openai_tokens() -> None:
    global _vault_cache
    _vault_cache = None


def api_key_source() -> ApiKeySource:
    if str(os.environ.get(API_KEY_ENV) or "").strip():
        return "environment"
    if _vault().get("api_key"):
        return "encrypted_store"
    return "none"


def api_key() -> str:
    """Resolve API key: environment override, then encrypted store, then empty."""
    env = str(os.environ.get(API_KEY_ENV) or "").strip()
    if env:
        return env
    return _vault().get("api_key") or ""


def mask_api_key(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "missing"
    if text.startswith(KEY_PREFIX):
        return f"{KEY_PREFIX}...configured"
    return "configured"


def token_report() -> dict[str, Any]:
    key = api_key()
    source = api_key_source()
    return {
        "enabled": openai_enabled(),
        "model": openai_model(),
        "api_key_configured": bool(key),
        "api_key_source": source,
        "api_key_valid_prefix": bool(key.startswith(KEY_PREFIX)) if key else False,
    }


def contains_secret(text: str) -> bool:
    key = api_key()
    return bool(key and key in str(text or ""))
