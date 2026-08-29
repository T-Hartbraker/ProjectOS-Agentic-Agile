"""Canonical Slack credential resolution for all ProjectOS consumers."""

from __future__ import annotations

import os
from typing import Any, Literal

from projectos.secret_store import read_slack_secrets

APP_TOKEN_ENV = "PROJECTOS_SLACK_APP_TOKEN"
BOT_TOKEN_ENV = "PROJECTOS_SLACK_BOT_TOKEN"
SIGNING_SECRET_ENV = "PROJECTOS_SLACK_SIGNING_SECRET"

APP_PREFIX = "xapp-"
BOT_PREFIX = "xoxb-"

TokenSource = Literal["environment", "encrypted_store", "none"]
ConnectionState = Literal[
    "not_configured",
    "configured_not_tested",
    "configured_invalid",
    "configured_connection_failed",
    "connected",
    "disabled",
]

_vault_cache: dict[str, str] | None = None


def _raw_env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _read_vault_fresh() -> dict[str, str]:
    return read_slack_secrets()


def _vault() -> dict[str, str]:
    global _vault_cache
    if _vault_cache is None:
        _vault_cache = _read_vault_fresh()
    return _vault_cache


def reload_slack_tokens() -> None:
    """Invalidate the in-process cache. Call after secret writes or before status reads."""
    global _vault_cache
    _vault_cache = None


def _token_source(vault_value: str, env_value: str) -> TokenSource:
    if env_value:
        return "environment"
    if vault_value:
        return "encrypted_store"
    return "none"


def resolve_slack_credentials(*, refresh: bool = False) -> dict[str, Any]:
    """Single authoritative Slack credential view for runtime, doctor, and dashboard."""
    if refresh:
        reload_slack_tokens()
    vault = _read_vault_fresh() if refresh else _vault()
    env_app = _raw_env(APP_TOKEN_ENV)
    env_bot = _raw_env(BOT_TOKEN_ENV)
    env_signing = _raw_env(SIGNING_SECRET_ENV)
    vault_app = str(vault.get("app_token") or "").strip()
    vault_bot = str(vault.get("bot_token") or "").strip()
    vault_signing = str(vault.get("signing_secret") or "").strip()

    app = env_app or vault_app
    bot = env_bot or vault_bot
    signing = env_signing or vault_signing

    app_source = _token_source(vault_app, env_app)
    bot_source = _token_source(vault_bot, env_bot)
    signing_source = _token_source(vault_signing, env_signing)

    app_valid = bool(app.startswith(APP_PREFIX)) if app else False
    bot_valid = bool(bot.startswith(BOT_PREFIX)) if bot else False

    configured = bool(app and bot)
    if not configured:
        connection_state: ConnectionState = "not_configured"
    elif not app_valid or not bot_valid:
        connection_state = "configured_invalid"
    else:
        connection_state = "configured_not_tested"

    if app_source == "encrypted_store" or bot_source == "encrypted_store":
        storage = "encrypted_local_store"
    elif app_source == "environment" or bot_source == "environment":
        storage = "environment"
    else:
        storage = "none"

    return {
        "app_token": app,
        "bot_token": bot,
        "signing_secret": signing,
        "app_token_present": bool(app),
        "bot_token_present": bool(bot),
        "app_token_valid_prefix": app_valid,
        "bot_token_valid_prefix": bot_valid,
        "signing_secret_present": bool(signing),
        "app_token_source": app_source,
        "bot_token_source": bot_source,
        "signing_secret_source": signing_source,
        "storage": storage,
        "configured": configured,
        "tokens_ready": configured and app_valid and bot_valid,
        "connection_state": connection_state,
    }


def app_token() -> str:
    creds = resolve_slack_credentials()
    return str(creds["app_token"] or "")


def bot_token() -> str:
    creds = resolve_slack_credentials()
    return str(creds["bot_token"] or "")


def signing_secret() -> str:
    creds = resolve_slack_credentials()
    return str(creds["signing_secret"] or "")


def mask_token(value: str | None, *, prefix: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "missing"
    if text.startswith(prefix):
        return f"{prefix}...configured"
    return "configured"


def token_report(*, refresh: bool = False) -> dict[str, Any]:
    creds = resolve_slack_credentials(refresh=refresh)
    return {
        "mode": "socket",
        "app_token": mask_token(creds["app_token"], prefix=APP_PREFIX),
        "bot_token": mask_token(creds["bot_token"], prefix=BOT_PREFIX),
        "app_token_present": creds["app_token_present"],
        "bot_token_present": creds["bot_token_present"],
        "app_token_valid_prefix": creds["app_token_valid_prefix"],
        "bot_token_valid_prefix": creds["bot_token_valid_prefix"],
        "signing_secret_present": creds["signing_secret_present"],
        "app_token_source": creds["app_token_source"],
        "bot_token_source": creds["bot_token_source"],
        "signing_secret_source": creds["signing_secret_source"],
        "storage": creds["storage"],
        "configured": creds["configured"],
        "tokens_ready": creds["tokens_ready"],
        "connection_state": creds["connection_state"],
    }


def secrets_for_runtime() -> dict[str, str]:
    creds = resolve_slack_credentials(refresh=True)
    return {
        "app_token": str(creds["app_token"] or ""),
        "bot_token": str(creds["bot_token"] or ""),
        "signing_secret": str(creds["signing_secret"] or ""),
    }


def contains_secret(text: str) -> bool:
    creds = resolve_slack_credentials(refresh=True)
    blob = str(text or "")
    for key in ("app_token", "bot_token", "signing_secret"):
        value = str(creds.get(key) or "")
        if value and value in blob:
            return True
    return False
