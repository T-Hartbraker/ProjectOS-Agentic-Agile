"""Save OpenAI API key to encrypted local storage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projectos.errors import OrchestrationError
from projectos.openai_client import probe_api
from projectos.openai_state import record_openai_result
from projectos.openai_tokens import KEY_PREFIX, api_key, reload_openai_tokens, token_report
from projectos.secret_store import delete_openai_api_key, write_openai_secrets


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def _validate_api_key(value: str) -> str:
    key = _clean(value)
    if not key:
        raise OrchestrationError("OpenAI API key is required when provided")
    if not key.startswith(KEY_PREFIX):
        raise OrchestrationError(f"OpenAI API key must start with {KEY_PREFIX}")
    return key


def apply_openai_secret(
    *,
    api_key_value: str | None = None,
    secrets_path: Path | str | None = None,
) -> dict[str, Any]:
    if api_key_value is None:
        raise OrchestrationError("OpenAI API key is required")
    key = _validate_api_key(api_key_value)
    write_openai_secrets({"api_key": key}, secrets_path=secrets_path, merge=True)
    reload_openai_tokens()
    report = token_report()
    return {
        "ok": True,
        "api_key_configured": report["api_key_configured"],
        "api_key_source": report["api_key_source"],
        "notice": (
            "OpenAI API key was saved securely on this PC. "
            "It persists across ProjectOS restarts and is never shown again in the dashboard."
        ),
    }


def remove_openai_secret(*, secrets_path: Path | str | None = None) -> dict[str, Any]:
    delete_openai_api_key(secrets_path=secrets_path)
    reload_openai_tokens()
    report = token_report()
    return {
        "ok": True,
        "api_key_configured": report["api_key_configured"],
        "api_key_source": report["api_key_source"],
        "notice": "Encrypted OpenAI API key removed from this PC. Environment override still applies if set.",
    }


def test_openai_connection(*, http_post=None) -> dict[str, Any]:
    if not api_key():
        raise OrchestrationError("OpenAI API key is not configured")
    try:
        result = probe_api(http_post=http_post)
    except Exception as exc:
        detail = str(exc)
        if api_key() and api_key() in detail:
            detail = "OpenAI connection test failed"
        record_openai_result(ok=False, detail=detail)
        return {"ok": False, "detail": detail, "response_id": None}
    record_openai_result(ok=True, detail="connection test ok", response_id=result.get("response_id"))
    return {
        "ok": True,
        "detail": "Connection successful",
        "response_id": result.get("response_id"),
    }
