"""Bounded authoritative Sponsor context for ChatGPT Advisor deliberation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from projectos.chatgpt_proposals import (
    ProposalRecord,
    get_latest_thread_proposal,
    proposal_awaiting_approval,
    proposal_lifecycle_label,
)
from projectos.db import connection
from projectos.domain_events import list_recent_events
from projectos.migrate import initialize_database
from projectos.presentation import queue_label, status_label
from projectos.projectctl_bridge import read_work_item_ids
from projectos.run_state import run_status_summary
from projectos.services.context import ServiceContext
from projectos.services.facades import ProjectQueryService
from projectos.qa_semantics import collect_assurance_facts, job_detail_facts
from projectos.sponsor_query import SponsorQueryService
from projectos.store import ACTIVE_WORKTREE_STATUSES


@dataclass
class SponsorContext:
    project_id: str
    project_name: str = ""
    project_goal: str = ""
    enabled: bool = True
    lifecycle_phase: str = ""
    health: str = ""
    iteration_id: str | None = None
    iteration_objective: str = ""
    iteration_status: str = ""
    active_work: list[str] = field(default_factory=list)
    recent_completions: list[str] = field(default_factory=list)
    backlog_summary: list[str] = field(default_factory=list)
    quality_summary: str = ""
    quality_failures: list[str] = field(default_factory=list)
    open_risks: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    sponsor_decisions: list[str] = field(default_factory=list)
    release_summary: str = ""
    release_readiness: str = ""
    learning_notes: list[str] = field(default_factory=list)
    quality_facts: dict[str, Any] = field(default_factory=dict)
    governance: dict[str, Any] = field(default_factory=dict)
    active_run_summary: str = ""
    recent_run_events: list[str] = field(default_factory=list)
    handoff_summary: str = ""
    terminal_run_summary: str = ""
    terminal_blocker: dict[str, Any] = field(default_factory=dict)
    known_unknowns: list[str] = field(default_factory=list)
    authoritative_job_facts: list[dict[str, Any]] = field(default_factory=list)

    def to_model_text(self) -> str:
        lines = [
            "=== AUTHORITATIVE PROJECTOS CONTEXT ===",
            "",
            "[AUTHORITATIVE FACTS]",
            f"Project ID: {self.project_id}",
        ]
        if self.project_name:
            lines.append(f"Name: {self.project_name}")
        if self.project_goal:
            lines.append(f"Goal/charter: {self.project_goal}")
        lines.extend(
            [
                f"Enabled: {self.enabled}",
                f"Lifecycle phase: {self.lifecycle_phase or 'unknown'}",
                f"Health: {self.health or 'unknown'}",
                "",
                "[ITERATION]",
                f"Current: {self.iteration_id or 'none identified'}",
            ]
        )
        if self.iteration_objective:
            lines.append(f"Objective: {self.iteration_objective}")
        if self.iteration_status:
            lines.append(f"Status: {self.iteration_status}")
        lines.append("")
        lines.append("[WORK]")
        if self.active_work:
            lines.append("Active:")
            lines.extend(f"- {item}" for item in self.active_work[:8])
        else:
            lines.append("Active: none running")
        if self.recent_completions:
            lines.append("Recent completions:")
            lines.extend(f"- {item}" for item in self.recent_completions[:6])
        if self.backlog_summary:
            lines.append("Backlog / known work items:")
            lines.extend(f"- {item}" for item in self.backlog_summary[:10])
        lines.extend(["", "[QUALITY]", self.quality_summary or "No quality data"])
        if self.quality_facts:
            lines.append("[QUALITY_FACTS_JSON]")
            lines.append(json.dumps(self.quality_facts, sort_keys=True))
            lines.append(
                "SEMANTICS: qa_jobs_* counts ASSURANCE orchestration jobs. "
                "assurance_evidence_rows_* counts qa_evidence table rows. "
                "These are NOT interchangeable. tests_* only when authoritative test data exists."
            )
        if self.quality_failures:
            lines.append("Failures/blockers:")
            lines.extend(f"- {item}" for item in self.quality_failures[:5])
        lines.append("")
        lines.append("[RISKS / BLOCKERS]")
        if self.open_risks:
            lines.extend(f"- {item}" for item in self.open_risks[:5])
        if self.blockers:
            lines.extend(f"- {item}" for item in self.blockers[:5])
        if not self.open_risks and not self.blockers:
            lines.append("- none identified")
        if self.sponsor_decisions:
            lines.append("")
            lines.append("[SPONSOR DECISIONS]")
            lines.extend(f"- {item}" for item in self.sponsor_decisions[:5])
        lines.extend(
            [
                "",
                "[RELEASE]",
                self.release_summary or "No release detail",
            ]
        )
        if self.release_readiness:
            lines.append(f"Readiness: {self.release_readiness}")
        if self.active_run_summary:
            lines.extend(["", "[ACTIVE RUN]", self.active_run_summary])
        if self.handoff_summary:
            lines.append(self.handoff_summary)
        if self.terminal_run_summary:
            lines.extend(["", "[TERMINAL RUN]", self.terminal_run_summary])
        if self.terminal_blocker:
            lines.extend(["", "[TERMINAL BLOCKER EVIDENCE]"])
            lines.append(json.dumps(self.terminal_blocker, sort_keys=True))
        if self.recent_run_events:
            lines.append("Recent run activity:")
            lines.extend(f"- {item}" for item in self.recent_run_events[:8])
        if self.authoritative_job_facts:
            lines.append("")
            lines.append("[AUTHORITATIVE JOB FACTS]")
            for item in self.authoritative_job_facts[:6]:
                lines.append(json.dumps(item, sort_keys=True))
        lines.append("")
        lines.append("[KNOWN UNKNOWNS]")
        if self.known_unknowns:
            lines.extend(f"- {item}" for item in self.known_unknowns[:8])
        else:
            lines.append("- none explicitly identified")
        lines.append("")
        lines.append("[ADVISOR INFERENCES]")
        lines.append(
            "- You may reason, but label inferences clearly and never present them as ProjectOS state."
        )
        lines.append(
            "- If blocker cause is unknown, say: ProjectOS does not contain enough evidence "
            "to determine the exact cause."
        )
        lines.append("")
        lines.append("[RECOMMENDATIONS]")
        lines.append("- Recommendations are not ProjectOS execution unless a handoff is accepted.")
        gov = self.governance
        if gov:
            lines.extend(
                [
                    "",
                    "[ACTIVE GOVERNANCE]",
                    f"Proposal: {gov.get('proposal_id') or 'none'}",
                    f"Lifecycle: {gov.get('lifecycle') or 'none'}",
                    f"Action: {gov.get('action_type') or 'none'}",
                    f"Approval required: {gov.get('approval_required', False)}",
                ]
            )
            if gov.get("human_summary"):
                lines.append(f"Summary: {gov['human_summary']}")
            if gov.get("latest_execution"):
                lines.append(f"Latest execution: {gov['latest_execution'][:500]}")
        if self.learning_notes:
            lines.append("")
            lines.append("[LEARNING / MEMORY]")
            lines.extend(f"- {item}" for item in self.learning_notes[:5])
        lines.append("")
        lines.append("=== END PROJECTOS CONTEXT ===")
        return "\n".join(lines)


def _governance_section(proposal: ProposalRecord | None) -> dict[str, Any]:
    if proposal is None:
        return {}
    return {
        "proposal_id": proposal.proposal_id,
        "lifecycle": proposal_lifecycle_label(proposal),
        "action_type": proposal.action_type,
        "approval_required": proposal_awaiting_approval(proposal),
        "human_summary": proposal.human_summary,
        "latest_execution": proposal.result_text,
        "has_preview": bool(proposal.preview_result),
    }


def build_sponsor_context(
    ctx: ServiceContext,
    conn,
    *,
    project_id: str,
    team_id: str,
    channel_id: str,
    thread_key: str,
    sponsor_user_id: str,
) -> SponsorContext:
    queries = ProjectQueryService(ctx)
    summary = queries.summary(project_id)
    current = queries.current(project_id)
    jobs = queries.jobs(project_id)
    quality = queries.quality(project_id)
    releases = queries.releases(project_id)

    running = [j for j in jobs if j.status in ACTIVE_WORKTREE_STATUSES]
    succeeded = [j for j in jobs if j.status == "SUCCEEDED"]
    failed = [j for j in jobs if j.status in {"FAILED", "BLOCKED"}]
    ready = [j for j in jobs if j.status in {"READY", "QUEUED"}]

    active_work = []
    for job in running[:6]:
        label = f"{job.human_id} ({queue_label(job.queue)})"
        if job.work_item_human_id:
            label += f" on {job.work_item_human_id}"
        active_work.append(label)

    recent_completions = []
    for job in reversed(succeeded[-8:]):
        recent_completions.append(f"{job.human_id} ({status_label(job.status)})")

    quality_jobs = [j for j in jobs if str(j.queue).startswith("ASSURANCE")]
    quality_facts = collect_assurance_facts(ctx, project_id)
    if quality_facts.get("reviews_total") is not None:
        quality_summary = (
            f"{quality_facts.get('reviews_completed', 0)} of {quality_facts['reviews_total']} "
            "assurance reviews completed"
        )
        if quality_facts.get("reviews_need_attention"):
            quality_summary += (
                f"; {quality_facts['reviews_need_attention']} need attention"
            )
    elif quality_facts.get("qa_jobs_total"):
        quality_summary = (
            f"{quality_facts.get('qa_jobs_completed', 0)} of {quality_facts['qa_jobs_total']} "
            "assurance jobs completed in scope"
        )
    elif quality_facts.get("assurance_evidence_rows_total"):
        quality_summary = (
            f"{quality_facts.get('assurance_evidence_rows_passed', 0)} of "
            f"{quality_facts['assurance_evidence_rows_total']} assurance evidence rows passed"
        )
    else:
        quality_summary = "No assurance jobs or evidence rows in current scope"

    handoff_summary = ""
    terminal_run_summary = ""
    terminal_blocker: dict[str, Any] = {}
    handoff_row = conn.execute(
        """
        SELECT handoff_id, request_type, status, run_id, objective
        FROM sponsor_handoffs
        WHERE project_id = ? AND channel_id = ? AND thread_ts = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (project_id, channel_id, thread_key),
    ).fetchone()
    if handoff_row:
        handoff_summary = (
            f"Handoff `{handoff_row['handoff_id']}`: "
            f"request_type={handoff_row['request_type']}, "
            f"status={handoff_row['status']}, run={handoff_row['run_id'] or 'none'}"
        )

    active_run_summary = ""
    recent_run_events: list[str] = []
    run_row = conn.execute(
        """
        SELECT run_id, status FROM execution_runs
        WHERE project_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if run_row:
        from projectos.run_evidence import build_terminal_evidence

        run_id = str(run_row["run_id"])
        run_status = str(run_row["status"])
        if run_status in {"BLOCKED", "FAILED", "COMPLETED", "CANCELLED", "ESCALATED"}:
            terminal = build_terminal_evidence(conn, run_id=run_id)
            if terminal:
                terminal_run_summary = (
                    f"Run {run_id}: terminal_status={terminal.get('terminal_status')}, "
                    f"request_type={terminal.get('request_type')}"
                )
                failure = terminal.get("failure") or {}
                if failure:
                    terminal_blocker = failure
                if terminal.get("result_summary"):
                    terminal_run_summary += f"; {terminal['result_summary'][:200]}"
        elif run_status in {"PLANNING", "WAITING_APPROVAL", "WAITING_FOR_SPONSOR", "RUNNING"}:
            summary = run_status_summary(conn, run_id=run_id)
            if summary:
                active_run_summary = (
                    f"Run {summary.get('run_id')}: status={summary.get('status')}, "
                    f"phase={summary.get('current_phase') or 'unknown'}, "
                    f"owner={summary.get('current_agent') or 'PM Agent'}, "
                    f"progress={summary.get('progress', 0)}%"
                )
                if summary.get("result_summary"):
                    active_run_summary += f"; {summary['result_summary'][:200]}"
        for evt in list_recent_events(conn, project_id=project_id, run_id=run_id, limit=8):
            recent_run_events.append(
                f"{evt.get('actor_role') or 'ProjectOS'}: {evt.get('summary') or evt.get('event_type')}"
            )

    blockers = []
    known_unknowns: list[str] = []
    authoritative_job_facts: list[dict[str, Any]] = []
    for job in failed[:8]:
        facts = job_detail_facts(ctx, project_id, job.human_id)
        authoritative_job_facts.append(facts)
        if facts.get("last_error"):
            blockers.append(f"{job.human_id} ({job.status}): {facts['last_error']}")
        else:
            blockers.append(f"{job.human_id} ({job.status})")
            if job.status in {"FAILED", "BLOCKED"}:
                known_unknowns.append(
                    f"{job.human_id}: status is {job.status} but ProjectOS does not record the exact blocker cause."
                )
    open_risks: list[str] = []
    for inv in quality.get("invalidations") or []:
        if isinstance(inv, dict):
            open_risks.append(str(inv.get("summary") or inv.get("code") or inv))

    sponsor_decisions: list[str] = []
    try:
        from projectos.decisions import list_decisions

        initialize_database(ctx.db_path)
        with connection(ctx.db_path) as inner:
            payload = list_decisions(inner, project_id, status="OPEN")
        for item in payload.get("decisions") or []:
            if isinstance(item, dict):
                sponsor_decisions.append(
                    f"{item.get('decision_human_id')}: {item.get('question') or item.get('title')}"
                )
    except Exception:
        pass

    release_lines = releases.get("releases") or []
    release_summary = "No releases recorded"
    release_readiness = ""
    if release_lines:
        latest = release_lines[-1] if isinstance(release_lines, list) else release_lines
        if isinstance(latest, dict):
            release_summary = (
                f"{latest.get('release_human_id') or latest.get('human_id')}: "
                f"{latest.get('status') or latest.get('state') or 'unknown'}"
            )
            release_readiness = str(latest.get("readiness") or latest.get("verification") or "")
        else:
            release_summary = str(latest)

    learning_notes: list[str] = []
    try:
        initialize_database(ctx.db_path)
        with connection(ctx.db_path) as inner:
            rows = inner.execute(
                """
                SELECT memory_human_id, summary, content
                FROM agent_memories
                WHERE project_human_id = ?
                ORDER BY updated_at DESC
                LIMIT 5
                """,
                (project_id,),
            ).fetchall()
        for row in rows:
            learning_notes.append(
                f"{row['memory_human_id']}: {str(row['summary'] or row['content'] or '')[:120]}"
            )
    except Exception:
        pass

    backlog_summary: list[str] = []
    try:
        entry = queries._require_project(project_id)
        known = read_work_item_ids(entry.repository_root)
        for kind, ids in sorted(known.items()):
            for hid in ids[:4]:
                backlog_summary.append(f"{kind} {hid}")
    except Exception:
        pass

    proposal = get_latest_thread_proposal(
        conn,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_key,
        sponsor_user_id=sponsor_user_id,
    )

    phase = "Active work" if running else ("Idle" if not ready else "Queued work waiting")
    health = "Healthy" if summary.enabled and not failed else "Needs attention"
    if not summary.enabled:
        health = "Paused"

    return SponsorContext(
        project_id=project_id,
        project_name=project_id,
        enabled=bool(summary.enabled),
        lifecycle_phase=phase,
        health=health,
        iteration_id=current.iteration_human_id,
        iteration_status="active" if running else "idle",
        active_work=active_work,
        recent_completions=recent_completions,
        backlog_summary=backlog_summary,
        quality_summary=quality_summary,
        quality_facts=quality_facts,
        quality_failures=[f"{j.human_id}" for j in failed if str(j.queue).startswith("ASSURANCE")],
        open_risks=open_risks,
        blockers=blockers,
        sponsor_decisions=sponsor_decisions,
        release_summary=release_summary,
        release_readiness=release_readiness,
        learning_notes=learning_notes,
        governance=_governance_section(proposal),
        active_run_summary=active_run_summary,
        recent_run_events=recent_run_events,
        handoff_summary=handoff_summary,
        terminal_run_summary=terminal_run_summary,
        terminal_blocker=terminal_blocker,
        known_unknowns=known_unknowns,
        authoritative_job_facts=authoritative_job_facts,
    )
