"""Encrypted local secret storage for ProjectOS integrations.

Secrets live under the ProjectOS state directory and survive software updates.
On Windows they are protected with DPAPI (user + machine bound). Values are never
returned by HTTP APIs or written to logs.

Unified secret identifiers use dotted names, e.g. ``openai.api_key``.
Legacy per-integration files are read for backward compatibility.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from projectos.errors import OrchestrationError
from projectos.paths import STATE_DIR

SLACK_SECRETS_FILE = "slack_secrets.enc"
OPENAI_SECRETS_FILE = "openai_secrets.enc"
PROJECTOS_SECRETS_FILE = "projectos_secrets.enc"
SLACK_SECRET_KEYS = ("app_token", "bot_token", "signing_secret")
OPENAI_SECRET_KEYS = ("api_key",)
OPENAI_API_KEY_ID = "openai.api_key"
_PLAIN_PREFIX = b"POV1:"

def _secrets_file() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / SLACK_SECRETS_FILE


def _openai_secrets_file() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / OPENAI_SECRETS_FILE


def _projectos_secrets_file() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / PROJECTOS_SECRETS_FILE


def _dpapi_protect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(raw: bytes) -> DATA_BLOB:
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    blob_in = _to_blob(data)
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise OrchestrationError("Could not encrypt secrets for local storage")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(raw: bytes) -> DATA_BLOB:
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    blob_in = _to_blob(data)
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise OrchestrationError("Could not decrypt stored secrets")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _encrypt(data: bytes) -> bytes:
    if sys.platform == "win32":
        return _dpapi_protect(data)
    import base64

    return _PLAIN_PREFIX + base64.b64encode(data)


def _decrypt(data: bytes) -> bytes:
    if data.startswith(_PLAIN_PREFIX):
        import base64

        return base64.b64decode(data[len(_PLAIN_PREFIX) :])
    if sys.platform == "win32":
        return _dpapi_unprotect(data)
    raise OrchestrationError("Stored secrets cannot be read on this platform")


def _read_encrypted_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = path.read_bytes()
    if not raw:
        return {}
    try:
        payload = json.loads(_decrypt(raw).decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_encrypted_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    path.write_bytes(encoded)


def read_projectos_secrets(*, secrets_path: Path | str | None = None) -> dict[str, str]:
    path = Path(secrets_path) if secrets_path is not None else _projectos_secrets_file()
    payload = _read_encrypted_json(path)
    out: dict[str, str] = {}
    for key, value in payload.items():
        text = str(value or "").strip()
        if text:
            out[str(key)] = text
    return out


def write_projectos_secret(
    secret_id: str,
    value: str,
    *,
    secrets_path: Path | str | None = None,
) -> None:
    key = str(secret_id or "").strip()
    text = str(value or "").strip()
    if not key or not text:
        raise OrchestrationError("Secret id and value are required")
    path = Path(secrets_path) if secrets_path is not None else _projectos_secrets_file()
    payload = _read_encrypted_json(path)
    payload[key] = text
    _write_encrypted_json(path, payload)


def delete_projectos_secret(
    secret_id: str,
    *,
    secrets_path: Path | str | None = None,
) -> None:
    key = str(secret_id or "").strip()
    if not key:
        raise OrchestrationError("Secret id is required")
    path = Path(secrets_path) if secrets_path is not None else _projectos_secrets_file()
    payload = _read_encrypted_json(path)
    if key not in payload:
        return
    del payload[key]
    if payload:
        _write_encrypted_json(path, payload)
    elif path.is_file():
        path.unlink()


def read_slack_secrets(*, secrets_path: Path | str | None = None) -> dict[str, str]:
    path = Path(secrets_path) if secrets_path is not None else _secrets_file()
    if not path.is_file():
        return {}
    raw = path.read_bytes()
    if not raw:
        return {}
    try:
        payload = json.loads(_decrypt(raw).decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for key in SLACK_SECRET_KEYS:
        value = str(payload.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def write_slack_secrets(
    updates: dict[str, str],
    *,
    secrets_path: Path | str | None = None,
    merge: bool = True,
) -> None:
    path = Path(secrets_path) if secrets_path is not None else _secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_slack_secrets(secrets_path=path) if merge else {}
    merged: dict[str, Any] = dict(current)
    for key, value in updates.items():
        if key not in SLACK_SECRET_KEYS:
            continue
        text = str(value or "").strip()
        if text:
            merged[key] = text
    encoded = _encrypt(json.dumps(merged, separators=(",", ":")).encode("utf-8"))
    path.write_bytes(encoded)


def clear_slack_secrets(*, secrets_path: Path | str | None = None) -> None:
    path = Path(secrets_path) if secrets_path is not None else _secrets_file()
    if path.is_file():
        path.unlink()


def read_openai_secrets(*, secrets_path: Path | str | None = None) -> dict[str, str]:
    projectos_path = Path(secrets_path) if secrets_path is not None else _projectos_secrets_file()
    unified = read_projectos_secrets(secrets_path=projectos_path)
    api_key = unified.get(OPENAI_API_KEY_ID, "")
    if api_key:
        return {"api_key": api_key}
    legacy_path = _openai_secrets_file()
    if secrets_path is not None:
        legacy_path = Path(secrets_path).parent / OPENAI_SECRETS_FILE
    payload = _read_encrypted_json(legacy_path)
    out: dict[str, str] = {}
    for key in OPENAI_SECRET_KEYS:
        value = str(payload.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def write_openai_secrets(
    updates: dict[str, str],
    *,
    secrets_path: Path | str | None = None,
    merge: bool = True,
) -> None:
    api_key = str(updates.get("api_key") or "").strip()
    projectos_path = Path(secrets_path) if secrets_path is not None else _projectos_secrets_file()
    if not api_key:
        if not merge:
            delete_openai_api_key(secrets_path=secrets_path)
        return
    write_projectos_secret(OPENAI_API_KEY_ID, api_key, secrets_path=projectos_path)
    legacy_path = _openai_secrets_file()
    if secrets_path is not None:
        legacy_path = Path(secrets_path).parent / OPENAI_SECRETS_FILE
    if legacy_path.is_file():
        legacy_path.unlink()


def delete_openai_api_key(*, secrets_path: Path | str | None = None) -> None:
    projectos_path = Path(secrets_path) if secrets_path is not None else _projectos_secrets_file()
    delete_projectos_secret(OPENAI_API_KEY_ID, secrets_path=projectos_path)
    legacy_path = _openai_secrets_file()
    if secrets_path is not None:
        legacy_path = Path(secrets_path).parent / OPENAI_SECRETS_FILE
    if legacy_path.is_file():
        legacy_path.unlink()


def clear_openai_secrets(*, secrets_path: Path | str | None = None) -> None:
    delete_openai_api_key(secrets_path=secrets_path)
