"""Advisor boundary error classification — do not mislabel ProjectOS faults as OpenAI failures."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

OPENAI_CONNECTION_ERROR = "OPENAI_CONNECTION_ERROR"
OPENAI_AUTH_ERROR = "OPENAI_AUTH_ERROR"
OPENAI_RATE_LIMIT = "OPENAI_RATE_LIMIT"
OPENAI_MODEL_ERROR = "OPENAI_MODEL_ERROR"
PROJECTOS_CONTEXT_ERROR = "PROJECTOS_CONTEXT_ERROR"
PROJECTOS_FORMATTER_ERROR = "PROJECTOS_FORMATTER_ERROR"
PROJECTOS_HANDOFF_ERROR = "PROJECTOS_HANDOFF_ERROR"
PROJECTOS_INTERNAL_ERROR = "PROJECTOS_INTERNAL_ERROR"

_STAGE_LABELS = {
    PROJECTOS_CONTEXT_ERROR: "Sponsor context assembly",
    PROJECTOS_FORMATTER_ERROR: "Sponsor context / release formatting",
    PROJECTOS_HANDOFF_ERROR: "PM handoff",
    PROJECTOS_INTERNAL_ERROR: "internal processing",
    OPENAI_CONNECTION_ERROR: "OpenAI connection",
    OPENAI_AUTH_ERROR: "OpenAI authentication",
    OPENAI_RATE_LIMIT: "OpenAI rate limit",
    OPENAI_MODEL_ERROR: "OpenAI model",
}


@dataclass(frozen=True)
class AdvisorError:
    error_class: str
    stage: str
    detail: str
    sponsor_message: str


def new_error_id() -> str:
    return f"ERR-{uuid.uuid4().hex[:10].upper()}"


def classify_advisor_exception(exc: BaseException, *, stage: str = "") -> AdvisorError:
    message = str(exc or "").strip()
    lowered = message.lower()
    if "format_releases" in lowered or "formatter" in lowered or "format_" in lowered:
        return AdvisorError(
            error_class=PROJECTOS_FORMATTER_ERROR,
            stage=stage or "Sponsor context / release formatting",
            detail=message[:500],
            sponsor_message=_sponsor_message_for(PROJECTOS_FORMATTER_ERROR),
        )
    if "handoff" in lowered:
        return AdvisorError(
            error_class=PROJECTOS_HANDOFF_ERROR,
            stage=stage or "PM handoff",
            detail=message[:500],
            sponsor_message=_sponsor_message_for(PROJECTOS_HANDOFF_ERROR),
        )
    if "context" in lowered or "sponsorcontext" in lowered:
        return AdvisorError(
            error_class=PROJECTOS_CONTEXT_ERROR,
            stage=stage or "Sponsor context assembly",
            detail=message[:500],
            sponsor_message=_sponsor_message_for(PROJECTOS_CONTEXT_ERROR),
        )
    if any(token in lowered for token in ("401", "unauthorized", "invalid api key", "authentication")):
        return AdvisorError(
            error_class=OPENAI_AUTH_ERROR,
            stage=stage or "OpenAI authentication",
            detail=message[:500],
            sponsor_message=_sponsor_message_for(OPENAI_AUTH_ERROR),
        )
    if any(token in lowered for token in ("429", "rate limit", "too many requests")):
        return AdvisorError(
            error_class=OPENAI_RATE_LIMIT,
            stage=stage or "OpenAI rate limit",
            detail=message[:500],
            sponsor_message=_sponsor_message_for(OPENAI_RATE_LIMIT),
        )
    if any(token in lowered for token in ("timeout", "connection", "network", "unreachable")):
        return AdvisorError(
            error_class=OPENAI_CONNECTION_ERROR,
            stage=stage or "OpenAI connection",
            detail=message[:500],
            sponsor_message=_sponsor_message_for(OPENAI_CONNECTION_ERROR),
        )
    if any(token in lowered for token in ("model", "responses api")):
        return AdvisorError(
            error_class=OPENAI_MODEL_ERROR,
            stage=stage or "OpenAI model",
            detail=message[:500],
            sponsor_message=_sponsor_message_for(OPENAI_MODEL_ERROR),
        )
    return AdvisorError(
        error_class=PROJECTOS_INTERNAL_ERROR,
        stage=stage or _STAGE_LABELS.get(PROJECTOS_INTERNAL_ERROR, "internal processing"),
        detail=message[:500],
        sponsor_message=_sponsor_message_for(PROJECTOS_INTERNAL_ERROR),
    )


def _sponsor_message_for(error_class: str) -> str:
    if error_class.startswith("OPENAI_"):
        return (
            "ChatGPT Advisor is currently unavailable due to an OpenAI integration issue. "
            "Check Settings → Integrations → OpenAI."
        )
    return (
        "ProjectOS Advisor encountered an internal ProjectOS error. "
        "No ProjectOS work was executed."
    )


def format_advisor_error_reply(error: AdvisorError, *, error_id: str) -> dict[str, Any]:
    stage = error.stage or _STAGE_LABELS.get(error.error_class, "unknown")
    lines = [
        "*ProjectOS Advisor*",
        "",
        error.sponsor_message,
        f"Stage: {stage}",
        f"Reference/error ID: `{error_id}`",
    ]
    if error.error_class.startswith("OPENAI_"):
        lines.append("Check Settings → Integrations → OpenAI.")
    else:
        lines.append("No ProjectOS mutation occurred. PM handoff was not completed unless stated below.")
    return {"text": "\n".join(lines), "response_type": "in_channel"}


def is_openai_error(error_class: str) -> bool:
    return str(error_class or "").startswith("OPENAI_")
