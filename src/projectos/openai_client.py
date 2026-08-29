"""OpenAI Responses API client. No local execution capability."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from projectos.errors import OrchestrationError
from projectos.openai_config import openai_model
from projectos.openai_state import record_openai_result
from projectos.openai_tokens import api_key, contains_secret

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_TIMEOUT_SECONDS = 45

HttpPost = Callable[[str, dict[str, str], dict[str, Any] | None], dict[str, Any]]


def default_http_post(url: str, headers: dict[str, str], body: dict[str, Any] | None) -> dict[str, Any]:
    payload = json.dumps(body or {}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {"error": {"message": raw[:240] or f"HTTP {exc.code}"}}
        if isinstance(data, dict):
            data["_http_status"] = exc.code
        return data if isinstance(data, dict) else {"ok": False, "error": {"message": "invalid_json"}}
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        return {"error": {"message": "invalid_json"}}
    return data


def _headers() -> dict[str, str]:
    key = api_key()
    if not key:
        raise OrchestrationError("OpenAI API key is not configured")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _safe_error_message(data: dict[str, Any]) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        message = str(err.get("message") or "OpenAI request failed")
    else:
        message = str(err or "OpenAI request failed")
    if contains_secret(message):
        return "OpenAI request failed (details redacted)"
    return message[:240]


def extract_output_text(data: dict[str, Any]) -> str:
    chunks: list[str] = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                        text = str(part.get("text") or "").strip()
                        if text:
                            chunks.append(text)
            text = str(item.get("text") or "").strip()
            if text:
                chunks.append(text)
    fallback = str(data.get("output_text") or "").strip()
    if fallback:
        chunks.append(fallback)
    return "\n".join(chunks).strip()


@dataclass(frozen=True)
class OpenAIResponse:
    response_id: str
    text: str
    raw: dict[str, Any]


def create_response(
    *,
    instructions: str,
    user_input: str,
    previous_response_id: str | None = None,
    model: str | None = None,
    http_post: HttpPost | None = None,
) -> OpenAIResponse:
    post = http_post or default_http_post
    body: dict[str, Any] = {
        "model": model or openai_model(),
        "instructions": instructions,
        "input": user_input,
        "store": True,
    }
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
    data = post(OPENAI_RESPONSES_URL, _headers(), body)
    if data.get("error") or not extract_output_text(data):
        message = _safe_error_message(data)
        record_openai_result(ok=False, detail=message)
        raise OrchestrationError(message)
    text = extract_output_text(data)
    response_id = str(data.get("id") or "").strip()
    record_openai_result(ok=True, detail="ok", response_id=response_id)
    return OpenAIResponse(response_id=response_id, text=text, raw=data)


def probe_api(*, http_post: HttpPost | None = None) -> dict[str, Any]:
    """Minimal authenticated request. Intended for doctor --probe only."""
    response = create_response(
        instructions="Reply with exactly: ok",
        user_input="ping",
        http_post=http_post,
    )
    return {"ok": True, "response_id": response.response_id, "text": response.text[:80]}
