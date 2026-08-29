"""Global OpenAI integration settings (non-secret)."""

from __future__ import annotations

from typing import Any

from projectos.openai_config import (
    SUPPORTED_OPENAI_MODELS,
    openai_enabled,
    openai_model,
    set_openai_model,
    validate_openai_model,
)
from projectos.openai_state import public_openai_state
from projectos.openai_tokens import token_report
from projectos.slack_chatgpt_config import (
    chatgpt_slack_user_id,
    chatgpt_slack_user_id_source,
    set_chatgpt_slack_user_id,
    validate_chatgpt_slack_user_id,
)


def _last_test_fields(state: dict[str, Any]) -> dict[str, Any]:
    last_ok = state.get("last_ok")
    if last_ok is True:
        status = "success"
    elif last_ok is False:
        status = "failed"
    else:
        status = "not_tested"
    last_error = None
    if last_ok is False:
        last_error = state.get("last_detail")
    return {
        "last_test_status": status,
        "last_test_at": state.get("updated_at"),
        "last_error": last_error,
    }


def read_openai_settings() -> dict[str, Any]:
    tokens = token_report()
    state = public_openai_state()
    test_fields = _last_test_fields(state)
    return {
        "enabled": bool(tokens["enabled"]),
        "api_key_configured": tokens["api_key_configured"],
        "api_key_source": tokens["api_key_source"],
        "model": tokens["model"] or openai_model(),
        "supported_models": list(SUPPORTED_OPENAI_MODELS),
        "slack_chatgpt_user_id": chatgpt_slack_user_id(),
        "slack_chatgpt_user_id_source": chatgpt_slack_user_id_source(),
        **test_fields,
        "setup_steps": [
            "Create an OpenAI API key with billing enabled (ChatGPT Plus does not include API credits).",
            "Enter the key below or set PROJECTOS_OPENAI_API_KEY on this PC (environment overrides the stored key).",
            "Run `python -m projectos openai doctor --probe` to verify the connection.",
            "In Slack, mention the installed ChatGPT app user (for example <@U…>) in an authorized ProjectOS channel.",
        ],
    }


def update_openai_settings(payload: dict[str, Any]) -> dict[str, Any]:
    model = payload.get("model")
    if model is not None:
        set_openai_model(validate_openai_model(str(model)))
    chatgpt_user = payload.get("slack_chatgpt_user_id")
    if chatgpt_user is not None:
        set_chatgpt_slack_user_id(str(chatgpt_user))
    return read_openai_settings()
