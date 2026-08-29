"""Slack Block Kit helpers for Sponsor console responses."""

from __future__ import annotations

from typing import Any

CHATGPT_HEADER = "*ChatGPT Advisor:*"
PROJECTOS_HEADER = "*ProjectOS:*"


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def _divider() -> dict[str, Any]:
    return {"type": "divider"}


def advisor_blocks(assessment: str, *, recommendation: str = "", decision: str = "") -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [_section(f"{CHATGPT_HEADER}\n\n*Assessment*\n{assessment}")]
    if recommendation:
        blocks.append(_section(f"*Recommendation*\n{recommendation}"))
    if decision:
        blocks.append(_section(f"*Decision needed*\n{decision}"))
    return blocks


def projectos_blocks(body: str) -> list[dict[str, Any]]:
    return [_section(f"{PROJECTOS_HEADER}\n\n{body}")]


def dual_response(
    *,
    advisor_text: str,
    projectos_text: str | None = None,
    advisor_recommendation: str = "",
    advisor_decision: str = "",
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = advisor_blocks(
        advisor_text,
        recommendation=advisor_recommendation,
        decision=advisor_decision,
    )
    fallback = f"{CHATGPT_HEADER}\n{advisor_text}"
    if projectos_text:
        blocks.append(_divider())
        blocks.extend(projectos_blocks(projectos_text))
        fallback = f"{fallback}\n\n{PROJECTOS_HEADER}\n{projectos_text}"
    return {
        "text": fallback[:3900],
        "blocks": blocks,
        "response_type": "in_channel",
    }


def single_advisor_response(text: str) -> dict[str, Any]:
    return dual_response(advisor_text=text)
