"""Save Slack tokens to encrypted local storage (one-time setup)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projectos.errors import OrchestrationError
from projectos.secret_store import write_slack_secrets
from projectos.slack_tokens import (
    APP_PREFIX,
    BOT_PREFIX,
    reload_slack_tokens,
    resolve_slack_credentials,
    token_report,
)

SLACK_SECRET_FIELDS = {
    "app_token": "app_token",
    "bot_token": "bot_token",
    "signing_secret": "signing_secret",
}


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def _validate_app_token(value: str) -> str:
    token = _clean(value)
    if not token:
        raise OrchestrationError("App token is required when provided")
    if not token.startswith(APP_PREFIX):
        raise OrchestrationError(f"App token must start with {APP_PREFIX}")
    return token


def _validate_bot_token(value: str) -> str:
    token = _clean(value)
    if not token:
        raise OrchestrationError("Bot token is required when provided")
    if not token.startswith(BOT_PREFIX):
        raise OrchestrationError(f"Bot token must start with {BOT_PREFIX}")
    return token


def _validate_signing_secret(value: str) -> str:
    secret = _clean(value)
    if not secret:
        raise OrchestrationError("Signing secret is required when provided")
    if len(secret) < 8:
        raise OrchestrationError("Signing secret is too short")
    return secret


def apply_slack_tokens(
    *,
    app_token: str | None = None,
    bot_token: str | None = None,
    signing_secret: str | None = None,
    secrets_path: Path | str | None = None,
) -> dict[str, Any]:
    updates: dict[str, str] = {}
    if app_token is not None and _clean(app_token):
        updates["app_token"] = _validate_app_token(app_token)
    if bot_token is not None and _clean(bot_token):
        updates["bot_token"] = _validate_bot_token(bot_token)
    if signing_secret is not None and _clean(signing_secret):
        updates["signing_secret"] = _validate_signing_secret(signing_secret)
    if not updates:
        raise OrchestrationError("Provide at least one token to save")

    write_slack_secrets(updates, secrets_path=secrets_path, merge=True)
    reload_slack_tokens()
    from projectos.slack_state import write_slack_state

    write_slack_state(
        {
            "status": "disconnected",
            "detail": "Tokens updated. Restart ProjectOS so the Slack adapter reconnects.",
        }
    )

    report = token_report(refresh=True)
    updated_fields = [key for key in SLACK_SECRET_FIELDS if key in updates]
    return {
        "ok": True,
        "updated_fields": updated_fields,
        "restart_required": True,
        "notice": (
            "Tokens were saved securely on this PC. "
            "They persist across ProjectOS updates. "
            "Restart ProjectOS once so the Slack adapter can connect."
        ),
        "storage": report.get("storage") or "encrypted_local_store",
        "app_token": report["app_token"],
        "bot_token": report["bot_token"],
        "app_token_present": report["app_token_present"],
        "bot_token_present": report["bot_token_present"],
        "app_token_valid_prefix": report["app_token_valid_prefix"],
        "bot_token_valid_prefix": report["bot_token_valid_prefix"],
        "signing_secret_present": report["signing_secret_present"],
        "app_token_source": report.get("app_token_source") or "none",
        "bot_token_source": report.get("bot_token_source") or "none",
    }


def probe_slack_connection(*, http_post=None) -> dict[str, Any]:
    creds = resolve_slack_credentials(refresh=True)
    if not creds["configured"]:
        raise OrchestrationError("Slack is not configured")
    if not creds["tokens_ready"]:
        raise OrchestrationError("Slack tokens are present but have invalid prefixes")

    from projectos.slack_socket import auth_identity, default_http_post, open_socket_url

    post = http_post or default_http_post
    results: dict[str, Any] = {
        "ok": True,
        "app_token": "FAIL",
        "bot_token": "FAIL",
        "socket_mode": "FAIL",
        "workspace": None,
        "team_id": None,
        "detail": "",
    }
    errors: list[str] = []

    try:
        open_socket_url(http_post=post)
        results["app_token"] = "PASS"
        results["socket_mode"] = "PASS"
    except OrchestrationError as exc:
        results["app_token"] = "FAIL"
        results["socket_mode"] = "FAIL"
        errors.append(f"app token: {exc}")

    try:
        identity = auth_identity(http_post=post)
        results["bot_token"] = "PASS"
        results["workspace"] = identity.get("workspace_name")
        results["team_id"] = identity.get("team_id")
    except OrchestrationError as exc:
        results["bot_token"] = "FAIL"
        errors.append(f"bot token: {exc}")

    if errors:
        results["ok"] = False
        results["detail"] = "; ".join(errors)
    else:
        results["detail"] = "Connection successful"
    return results
