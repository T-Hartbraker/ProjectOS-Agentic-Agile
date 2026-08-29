"""Canonical GitHub credential resolution."""

from __future__ import annotations

import os
from typing import Any, Literal

from projectos.secret_store import read_projectos_secrets

TOKEN_ENV = "PROJECTOS_GITHUB_TOKEN"
TOKEN_SECRET_ID = "github.token"

TokenSource = Literal["environment", "encrypted_store", "none"]

_vault_cache: dict[str, str] | None = None


def reload_github_tokens() -> None:
    global _vault_cache
    _vault_cache = None


def _vault() -> dict[str, str]:
    global _vault_cache
    if _vault_cache is None:
        _vault_cache = read_projectos_secrets()
    return _vault_cache


def resolve_github_credentials(*, refresh: bool = False) -> dict[str, Any]:
    if refresh:
        reload_github_tokens()
    vault = read_projectos_secrets() if refresh else _vault()
    env_token = str(os.environ.get(TOKEN_ENV) or "").strip()
    vault_token = str(vault.get(TOKEN_SECRET_ID) or "").strip()
    token = env_token or vault_token
    if env_token:
        source: TokenSource = "environment"
        storage = "environment"
    elif vault_token:
        source = "encrypted_store"
        storage = "encrypted_local_store"
    else:
        source = "none"
        storage = "none"
    valid = bool(token.startswith("ghp_") or token.startswith("github_pat_") or token.startswith("gho_"))
    return {
        "token": token,
        "token_present": bool(token),
        "token_valid_prefix": valid if token else False,
        "token_source": source,
        "storage": storage,
        "configured": bool(token),
    }


def github_token() -> str:
    return str(resolve_github_credentials().get("token") or "")


def contains_secret(text: str) -> bool:
    token = github_token()
    return bool(token and token in str(text or ""))
