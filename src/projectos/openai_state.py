"""Persisted OpenAI API diagnostics. No secrets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from projectos.paths import STATE_DIR

_STATE_PATH = STATE_DIR / "openai_state.json"


def _read() -> dict[str, Any]:
    if not _STATE_PATH.is_file():
        return {}
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def record_openai_result(
    *,
    ok: bool,
    detail: str,
    response_id: str | None = None,
) -> None:
    from datetime import datetime, timezone

    current = _read()
    current.update(
        {
            "last_ok": bool(ok),
            "last_detail": str(detail or "")[:240],
            "last_response_id": response_id,
            "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
    )
    _write(current)


def public_openai_state() -> dict[str, Any]:
    data = _read()
    return {
        "last_ok": data.get("last_ok"),
        "last_detail": data.get("last_detail"),
        "last_response_id": data.get("last_response_id"),
        "updated_at": data.get("updated_at"),
    }


def connection_status() -> str:
    data = _read()
    last_ok = data.get("last_ok")
    if last_ok is True:
        return "success"
    if last_ok is False:
        return "failed"
    return "not tested"
