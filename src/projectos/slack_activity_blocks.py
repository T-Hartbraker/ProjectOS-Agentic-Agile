"""Block Kit rendering for ProjectOS agent activity in Sponsor threads."""

from __future__ import annotations

from typing import Any


def handoff_accepted_blocks(
    *,
    handoff_id: str,
    project_id: str,
    request_type: str,
    run_id: str,
    objective: str,
) -> list[dict[str, Any]]:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "ProjectOS PM — HANDOFF ACCEPTED"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Handoff:*\n`{handoff_id}`"},
                {"type": "mrkdwn", "text": f"*Project:*\n`{project_id}`"},
                {"type": "mrkdwn", "text": f"*Request:*\n`{request_type}`"},
                {"type": "mrkdwn", "text": f"*Run:*\n`{run_id}`"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Objective*\n{objective[:1500]}"},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "State: `PLANNING` — PM Agent owns orchestration from here."},
            ],
        },
    ]


def handoff_failed_blocks(*, handoff_id: str | None, reason: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "ProjectOS — HANDOFF FAILED"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    (f"Handoff: `{handoff_id}`\n" if handoff_id else "")
                    + f"*Reason:* {reason[:1500]}"
                ),
            },
        },
    ]


def pm_plan_blocks(
    *,
    run_id: str,
    project_id: str,
    objective: str,
    phases: list[tuple[str, str]],
    current: str,
) -> list[dict[str, Any]]:
    plan_lines = []
    for label, state in phases:
        icon = {"done": "✓", "active": "→", "pending": "○"}.get(state, "○")
        plan_lines.append(f"{icon} {label}")
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*PM Agent — `{run_id}`*\n━━━━━━━━━━━━━━━━━━",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Objective*\n{objective[:1200]}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Plan*\n" + "\n".join(plan_lines)},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Current*\n{current[:800]}"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Project: `{project_id}`"}],
        },
    ]


def agent_activity_blocks(
    *,
    actor_role: str,
    run_id: str | None,
    summary: str,
    detail: str = "",
    evidence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    title = f"*{actor_role}*"
    if run_id:
        title += f" — `{run_id}`"
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": title}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary[:2800]}},
    ]
    if detail:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": detail[:2800]}}
        )
    if evidence:
        lines = [f"*{key}:* `{value}`" for key, value in list(evidence.items())[:8]]
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Evidence*\n" + "\n".join(lines)}}
        )
    return blocks


def decision_required_blocks(
    *,
    run_id: str,
    agent_role: str,
    question: str,
    options: list[str],
) -> list[dict[str, Any]]:
    option_text = "\n".join(f"• {opt}" for opt in options[:6])
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "ProjectOS — DECISION REQUIRED"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Run:*\n`{run_id}`"},
                {"type": "mrkdwn", "text": f"*Agent:*\n{agent_role}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Decision*\n{question[:1500]}"},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Options*\n{option_text}"}},
    ]


def activity_event_to_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    event_type = str(event.get("event_type") or "")
    if event_type == "HANDOFF_ACCEPTED":
        meta = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        return handoff_accepted_blocks(
            handoff_id=str(event.get("handoff_id") or ""),
            project_id=str(event.get("project_id") or ""),
            request_type=str(event.get("request_type") or meta.get("request_type") or ""),
            run_id=str(event.get("run_id") or ""),
            objective=str(event.get("summary") or event.get("objective") or ""),
        )
    if event_type == "PM_PLAN_CREATED":
        meta = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        return pm_plan_blocks(
            run_id=str(event.get("run_id") or ""),
            project_id=str(event.get("project_id") or ""),
            objective=str(event.get("objective") or event.get("summary") or ""),
            phases=list(event.get("phases") or meta.get("phases") or []),
            current=str(event.get("detail") or ""),
        )
    if event_type == "SPONSOR_DECISION_REQUIRED":
        return decision_required_blocks(
            run_id=str(event.get("run_id") or ""),
            agent_role=str(event.get("actor_role") or "PM Agent"),
            question=str(event.get("summary") or ""),
            options=list(event.get("options") or []),
        )
    evidence = event.get("evidence")
    return agent_activity_blocks(
        actor_role=str(event.get("actor_role") or "ProjectOS"),
        run_id=str(event.get("run_id") or "") or None,
        summary=str(event.get("summary") or ""),
        detail=str(event.get("detail") or ""),
        evidence=evidence if isinstance(evidence, dict) else None,
    )
