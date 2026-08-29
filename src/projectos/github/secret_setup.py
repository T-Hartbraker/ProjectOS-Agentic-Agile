"""GitHub settings and secret setup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projectos.errors import OrchestrationError
from projectos.github.client import GitHubClient
from projectos.github.tokens import reload_github_tokens, resolve_github_credentials
from projectos.secret_store import delete_projectos_secret, write_projectos_secret

GITHUB_TOKEN_ID = "github.token"
VALID_PREFIXES = ("ghp_", "github_pat_", "gho_")


def _validate_token(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        raise OrchestrationError("GitHub token is required when provided")
    if not token.startswith(VALID_PREFIXES):
        raise OrchestrationError("GitHub token must start with ghp_, github_pat_, or gho_")
    return token


def apply_github_token(*, token_value: str | None = None, secrets_path: Path | str | None = None) -> dict[str, Any]:
    if token_value is None or not str(token_value).strip():
        raise OrchestrationError("GitHub token is required")
    token = _validate_token(token_value)
    write_projectos_secret(GITHUB_TOKEN_ID, token, secrets_path=secrets_path)
    reload_github_tokens()
    creds = resolve_github_credentials(refresh=True)
    return {
        "ok": True,
        "configured": creds["configured"],
        "token_source": creds["token_source"],
        "notice": "GitHub token saved securely. It is never shown again in the dashboard.",
    }


def remove_github_token(*, secrets_path: Path | str | None = None) -> dict[str, Any]:
    delete_projectos_secret(GITHUB_TOKEN_ID, secrets_path=secrets_path)
    reload_github_tokens()
    creds = resolve_github_credentials(refresh=True)
    return {
        "ok": True,
        "configured": creds["configured"],
        "token_source": creds["token_source"],
        "notice": "Encrypted GitHub token removed from this PC.",
    }


def probe_github_connection(*, http_post=None) -> dict[str, Any]:
    creds = resolve_github_credentials(refresh=True)
    if not creds["configured"]:
        raise OrchestrationError("GitHub is not configured")
    client = GitHubClient(http_post=http_post)

    def _get(url, headers, body, method="GET"):
        return (http_post or client._http_post)(url, headers, body, method)

    token = creds["token"]
    from projectos.github.client import _headers

    data = _get(f"https://api.github.com/user", _headers(token), None, "GET")
    if data.get("message") and not data.get("login"):
        return {"ok": False, "detail": str(data.get("message")), "login": None}
    return {
        "ok": True,
        "detail": "Connection successful",
        "login": data.get("login"),
    }


def read_github_settings() -> dict[str, Any]:
    creds = resolve_github_credentials(refresh=True)
    return {
        "configured": creds["configured"],
        "token_configured": creds["token_present"],
        "token_source": creds["token_source"],
        "token_valid_prefix": creds["token_valid_prefix"],
        "storage": creds["storage"],
    }
