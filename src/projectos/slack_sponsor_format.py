"""Deterministic Sponsor-facing formatting for Slack ChatGPT workflow."""

from __future__ import annotations

from typing import Any

from projectos.chatgpt_proposals import ProposalRecord, is_work_mutation, proposal_lifecycle_label
from projectos.db import connection
from projectos.intake import IntakeResult
from projectos.migrate import initialize_database
from projectos.services.context import ServiceContext
from projectos.slack_sponsor_context import build_sponsor_context as build_sponsor_context_full

SPONSOR_ACCEPTANCE = (
    "Sponsor accepts when ProjectOS records the requested change as complete."
)


def _plan_jobs(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    jobs = plan.get("jobs")
    return [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []


def _iteration_from_plan(plan: dict[str, Any] | None) -> str:
    if not isinstance(plan, dict):
        return ""
    return str(plan.get("iteration_human_id") or "").strip()


def _job_lines(expected_jobs: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for job in expected_jobs[:8]:
        hid = str(job.get("human_id") or "").strip()
        queue = str(job.get("queue") or "").strip()
        role = str(job.get("agent_role") or "").strip()
        work_item = str(job.get("work_item_human_id") or "").strip()
        work_type = str(job.get("work_item_type") or "").strip()
        parts = [part for part in (hid, queue, role) if part]
        line = " / ".join(parts) if parts else "planned job"
        if work_item:
            line += f" → {work_type or 'item'} {work_item}".strip()
        lines.append(f"- {line}")
    return lines


def format_work_intake_preview(result: IntakeResult, proposal: ProposalRecord) -> str:
    """Rich deterministic preview — only fields ProjectOS can authoritatively determine."""
    lines = [
        "*ProjectOS — PREVIEW*",
        "_No changes applied. This is a read-only preview._",
        "",
        f"*Proposal:* `{proposal.proposal_id}`",
        f"*Project:* {proposal.project_human_id}",
        f"*Action:* {proposal.action_type}",
        f"*State:* {proposal_lifecycle_label(proposal)}",
        f"*Risk:* {proposal.risk.upper()}",
        "",
        "*Proposed change*",
        proposal.human_summary or proposal.instruction,
        "",
        f"*Preview status:* {result.status}",
    ]
    iteration = _iteration_from_plan(result.plan)
    if iteration:
        lines.append(f"*Iteration:* {iteration}")
    if result.plan_source:
        lines.append(f"*Plan source:* {result.plan_source}")

    if result.expected_jobs:
        lines.extend(["", "*Expected orchestration jobs*", *_job_lines(result.expected_jobs)])
    elif _plan_jobs(result.plan):
        lines.extend(
            [
                "",
                "*Expected orchestration jobs*",
                *_job_lines(_plan_jobs(result.plan)),
            ]
        )

    if result.assumptions:
        lines.append("")
        lines.append("*Assumptions*")
        for item in result.assumptions[:5]:
            lines.append(f"- {item.get('code')}: {item.get('statement')}")

    if result.decision_requests:
        lines.append("")
        lines.append("*Sponsor decisions required before submit*")
        for item in result.decision_requests[:5]:
            question = item.get("question") or item.get("statement") or "Decision required"
            lines.append(f"- {item.get('code')}: {question}")

    lines.extend(
        [
            "",
            "*Will change*",
            "- Orchestration job graph (if approved and submitted)",
            "- Project work tracking state via ProjectOS intake",
            "",
            "*Will not change*",
            "- No files modified during preview",
            "- No jobs created until Sponsor approval and execution",
            "- No release or packaging actions",
            "",
            "*Approval*",
            "Reply `Approved` or `Execute it` to run this exact proposal.",
        ]
    )
    if result.error:
        lines.extend(["", f"*Preview note:* {result.error}"])
    return "\n".join(lines)


def format_work_intake_execution(result: IntakeResult, proposal: ProposalRecord) -> str:
    lines = [
        "*ProjectOS — EXECUTED*",
        "",
        f"*Proposal:* `{proposal.proposal_id}`",
        f"*Project:* {proposal.project_human_id}",
        f"*Action:* {proposal.action_type}",
        f"*Outcome status:* {result.status}",
    ]
    iteration = _iteration_from_plan(result.plan)
    if iteration:
        lines.append(f"*Iteration:* {iteration}")
    if result.jobs_created:
        lines.append("")
        lines.append("*Jobs created*")
        for job_id in result.jobs_created[:10]:
            lines.append(f"- `{job_id}`")
    elif result.expected_jobs:
        lines.append("")
        lines.append("*Planned jobs (none persisted)*")
        lines.extend(_job_lines(result.expected_jobs))
    else:
        lines.append("")
        lines.append("*Jobs created:* none")

    work_items = []
    for job in result.expected_jobs:
        wid = str(job.get("work_item_human_id") or "").strip()
        if wid:
            work_items.append(wid)
    for job in _plan_jobs(result.plan):
        wid = str(job.get("work_item_human_id") or "").strip()
        if wid and wid not in work_items:
            work_items.append(wid)
    if work_items:
        lines.append("")
        lines.append("*Referenced work items*")
        for wid in work_items[:10]:
            lines.append(f"- `{wid}`")

    lines.extend(
        [
            "",
            "*Files modified:* none (intake submission only)",
            "",
            "*Evidence*",
            f"- proposal `{proposal.proposal_id}`",
            f"- execution status `{result.status}`",
        ]
    )
    if result.jobs_created:
        lines.append(f"- jobs: {', '.join(result.jobs_created[:10])}")
    if result.error:
        lines.append(f"- note: {result.error}")
    return "\n".join(lines)


def format_proposal_created_advisor(proposal: ProposalRecord) -> str:
    return (
        "I created a governed ProjectOS proposal for your review. "
        "ProjectOS generated a deterministic preview below — no project changes were applied. "
        "Reply `Approved` when you want ProjectOS to execute the exact proposal."
    )


def format_preview_complete_advisor(proposal: ProposalRecord) -> str:
    return (
        f"Preview is ready for proposal `{proposal.proposal_id}`. "
        "No project changes were applied. "
        "Reply `Approved` or `Execute it` to run the exact persisted action."
    )


def format_execution_complete_advisor(proposal: ProposalRecord) -> str:
    if is_work_mutation(proposal.action_type):
        return (
            f"ProjectOS executed the approved proposal `{proposal.proposal_id}` "
            f"for {proposal.project_human_id}. See the execution evidence below."
        )
    return f"ProjectOS completed the approved action for {proposal.project_human_id}."


def build_sponsor_context(ctx: ServiceContext, project_id: str) -> str:
    """Backward-compatible text context helper."""
    initialize_database(ctx.db_path)
    with connection(ctx.db_path) as conn:
        sponsor_ctx = build_sponsor_context_full(
            ctx,
            conn,
            project_id=project_id,
            team_id="",
            channel_id="",
            thread_key="",
            sponsor_user_id="",
        )
    return sponsor_ctx.to_model_text()
