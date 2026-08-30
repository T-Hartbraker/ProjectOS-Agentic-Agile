"""Read-only Sponsor fact retrieval — no mutations."""

from __future__ import annotations

import json
from typing import Any

from projectos.db import connection
from projectos.operational_failure import format_sponsor_failure_explanation
from projectos.migrate import initialize_database
from projectos.run_state import run_status_summary
from projectos.services.context import ServiceContext
from projectos.services.facades import ProjectQueryService
from projectos.qa_semantics import collect_assurance_facts, job_detail_facts
from projectos.slack_replies import format_quality, format_releases, format_summary
from projectos.store import ACTIVE_WORKTREE_STATUSES


class SponsorQueryService:
    """Explicit query operations for Advisor read-only context."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def get_project_status(self, project_id: str) -> str:
        return format_summary(self.ctx, project_id)

    def get_quality_summary(self, project_id: str) -> tuple[str, dict[str, Any]]:
        text = format_quality(self.ctx, project_id)
        facts = collect_assurance_facts(self.ctx, project_id)
        return text, facts

    def get_job_detail(self, project_id: str, job_human_id: str) -> str:
        facts = job_detail_facts(self.ctx, project_id, job_human_id)
        if not facts.get("known"):
            return (
                f"Job {job_human_id}: not found in ProjectOS scope for {project_id}. "
                f"{facts.get('unknown_reason', '')}"
            )
        lines = [
            f"Job: {facts['job_human_id']}",
            f"Queue: {facts.get('queue')}",
            f"Status: {facts.get('status')}",
        ]
        if facts.get("work_item_human_id"):
            lines.append(f"Work item: {facts['work_item_human_id']}")
        if facts.get("last_error"):
            lines.append(f"Last error (authoritative): {facts['last_error']}")
        else:
            lines.append(
                "Blocker cause: ProjectOS does not contain enough evidence to determine the exact cause."
            )
        if facts.get("outcome"):
            lines.append(f"Outcome: {facts['outcome']}")
        return "\n".join(lines)

    def get_release_summary(self, project_id: str, *, raw_text: str = "") -> str:
        return format_releases(self.ctx, project_id, raw_text=raw_text)

    def get_iteration_status(self, project_id: str) -> str:
        queries = ProjectQueryService(self.ctx)
        current = queries.current(project_id)
        if not current.iteration_human_id:
            return "No active iteration identified."
        return (
            f"Iteration {current.iteration_human_id}: "
            f"{current.iteration_objective or 'no objective recorded'}"
        )

    def get_run_status(self, project_id: str, *, run_id: str | None = None) -> str:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            if run_id:
                summary = run_status_summary(conn, run_id=run_id)
            else:
                row = conn.execute(
                    """
                    SELECT run_id FROM execution_runs
                    WHERE project_id = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (project_id,),
                ).fetchone()
                if not row:
                    return "No execution run recorded for this project."
                summary = run_status_summary(conn, run_id=str(row["run_id"]))
        if not summary:
            return "No execution run state available."
        lines = [
            f"Run: {summary.get('run_id')}",
            f"Status: {summary.get('status')}",
            f"Phase: {summary.get('current_phase') or 'unknown'}",
            f"Owner: {summary.get('current_agent') or 'PM Agent'}",
            f"Progress: {summary.get('progress', 0)}%",
        ]
        if summary.get("result_summary"):
            lines.append(f"Summary: {summary['result_summary']}")
        recent = summary.get("recent_events") or []
        if recent:
            lines.append("Recent activity:")
            for evt in recent[:5]:
                lines.append(f"- {evt.get('actor_role')}: {evt.get('summary')}")
        return "\n".join(lines)

    def get_artifact_summary(self, project_id: str) -> str:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            rows = conn.execute(
                """
                SELECT a.artifact_id, a.artifact_name, a.sha256, a.size_bytes, r.release_human_id
                FROM delivery_artifacts a
                JOIN delivery_releases r ON r.release_record_id = a.release_record_id
                WHERE a.project_human_id = ?
                ORDER BY a.created_at DESC
                LIMIT 5
                """,
                (project_id,),
            ).fetchall()
        if not rows:
            return "No delivery artifacts recorded."
        lines = ["Recent artifacts:"]
        for row in rows:
            lines.append(
                f"- {row['release_human_id']}: {row['artifact_name']} "
                f"sha256={row['sha256'][:12]}... size={row['size_bytes']}"
            )
        return "\n".join(lines)

    def get_risk_summary(self, project_id: str) -> str:
        queries = ProjectQueryService(self.ctx)
        quality = queries.quality(project_id)
        invalidations = quality.get("invalidations") or []
        if not invalidations:
            return "No open risks identified in ProjectOS quality state."
        lines = ["Open risks/blockers:"]
        for inv in invalidations[:5]:
            if isinstance(inv, dict):
                lines.append(f"- {inv.get('summary') or inv.get('code') or inv}")
            else:
                lines.append(f"- {inv}")
        return "\n".join(lines)

    def get_blocker_summary(self, project_id: str) -> str:
        """Authoritative blocker answer from terminal run evidence and latest failure events."""
        from projectos.run_evidence import build_terminal_evidence

        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            row = conn.execute(
                """
                SELECT run_id, status FROM execution_runs
                WHERE project_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if not row:
                return "No execution run recorded for this project."
            run_id = str(row["run_id"])
            terminal = build_terminal_evidence(conn, run_id=run_id)
            failure = terminal.get("failure") or {}
            lines = [
                f"Run: {run_id}",
                f"Status: {terminal.get('terminal_status') or row['status']}",
            ]
            if failure.get("blocker_type"):
                lines.append(f"Blocker type: {failure['blocker_type']}")
            if failure.get("path"):
                lines.append(f"Missing path: {failure['path']}")
            if failure.get("required_action"):
                lines.append(f"Required action: {failure['required_action']}")
            elif failure.get("reason"):
                lines.append(f"Reason: {failure['reason']}")
            qa = terminal.get("qa") or failure.get("qa") or {}
            if qa.get("reviews_total") is not None:
                lines.append(
                    f"QA gate: {qa.get('reviews_completed', 0)} completed, "
                    f"{qa.get('reviews_need_attention', 0)} need attention "
                    f"of {qa['reviews_total']} reviews (gate={qa.get('gate', 'unknown')})"
                )
            if failure.get("auto_remediation", {}).get("available"):
                lines.append(
                    "Auto-remediation: ProjectOS may generate a governed delivery contract draft "
                    "after Sponsor confirms repository owner/name and signing policy."
                )
            if not failure and terminal.get("terminal_status") not in {"BLOCKED", "FAILED"}:
                return self.get_run_status(project_id, run_id=run_id)
            return "\n".join(lines)

    def get_failure_explanation(
        self,
        project_id: str,
        *,
        run_id: str | None = None,
        thread_key: str | None = None,
    ) -> str:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            resolved_run_id = run_id
            if not resolved_run_id and thread_key:
                row = conn.execute(
                    """
                    SELECT run_id FROM sponsor_handoffs
                    WHERE project_id = ? AND thread_ts = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (project_id, thread_key),
                ).fetchone()
                if row and row["run_id"]:
                    resolved_run_id = str(row["run_id"])
            if not resolved_run_id:
                row = conn.execute(
                    """
                    SELECT run_id FROM execution_runs
                    WHERE project_id = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (project_id,),
                ).fetchone()
                if row:
                    resolved_run_id = str(row["run_id"])
            if not resolved_run_id:
                return (
                    "No execution run is recorded for this project/thread, so ProjectOS "
                    "does not contain authoritative failure evidence to explain."
                )

            failure_row = conn.execute(
                """
                SELECT summary, detail, evidence_json, metadata_json, occurred_at
                FROM projectos_events
                WHERE run_id = ? AND event_type = 'OPERATION_FAILED'
                ORDER BY occurred_at DESC
                LIMIT 1
                """,
                (resolved_run_id,),
            ).fetchone()
            if failure_row is None:
                run = conn.execute(
                    """
                    SELECT status, result_summary, evidence_json
                    FROM execution_runs WHERE run_id = ?
                    """,
                    (resolved_run_id,),
                ).fetchone()
                if run and str(run["result_summary"] or "").strip():
                    return (
                        f"Run: {resolved_run_id}\n"
                        f"Status: {run['status']}\n"
                        f"Summary: {run['result_summary']}"
                    )
                return (
                    f"Run `{resolved_run_id}` exists, but ProjectOS does not contain "
                    "authoritative failure evidence for this thread. "
                    "I cannot invent a cause."
                )

            evidence: dict[str, Any] = {}
            for raw in (failure_row["evidence_json"], failure_row["metadata_json"]):
                if not raw:
                    continue
                try:
                    parsed = json.loads(str(raw))
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    evidence.update(parsed)
            if not evidence:
                evidence = {
                    "error_detail": str(failure_row["detail"] or failure_row["summary"] or ""),
                    "error_category": "operational_failure",
                    "recoverable": True,
                }
            recovery = conn.execute(
                """
                SELECT action_type, status FROM run_next_actions
                WHERE run_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (resolved_run_id,),
            ).fetchone()
            recovery_summary = ""
            if recovery:
                recovery_summary = (
                    f"Recovery: durable next action `{recovery['action_type']}` "
                    f"is pending for run `{resolved_run_id}`."
                )
            return format_sponsor_failure_explanation(
                project_id=project_id,
                run_id=resolved_run_id,
                evidence=evidence,
                recovery_summary=recovery_summary,
            )

    def query_for_advisor(
        self,
        project_id: str,
        intent: str,
        *,
        raw_text: str = "",
        thread_key: str | None = None,
    ) -> str:
        intent = (intent or "summary").lower()
        if intent in {"failure", "explain_failure", "why_failed"}:
            return self.get_failure_explanation(
                project_id,
                thread_key=thread_key,
            )
        if intent in {"blocker", "blockers", "blocked"}:
            return self.get_blocker_summary(project_id)
        if intent in {"quality", "qa"}:
            text, _ = self.get_quality_summary(project_id)
            return text
        if intent in {"releases", "release"}:
            return self.get_release_summary(project_id, raw_text=raw_text)
        if intent == "job":
            job_id = self._job_id_from_text(raw_text)
            if job_id:
                return self.get_job_detail(project_id, job_id)
        if intent == "iteration":
            return self.get_iteration_status(project_id)
        if intent == "run":
            return self.get_run_status(project_id)
        if intent == "artifacts":
            return self.get_artifact_summary(project_id)
        if intent == "risks":
            return self.get_risk_summary(project_id)
        return self.get_project_status(project_id)

    @staticmethod
    def _job_id_from_text(text: str) -> str | None:
        import re

        match = re.search(r"\b(JOB-[A-Z0-9_-]+)\b", str(text or ""), re.IGNORECASE)
        return match.group(1).upper() if match else None

    def quality_facts_json(self, project_id: str) -> str:
        return json.dumps(collect_assurance_facts(self.ctx, project_id), sort_keys=True)
