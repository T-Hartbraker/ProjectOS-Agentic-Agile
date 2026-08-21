"""Budget / run accounting — never fabricate token usage."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.paths import DEFAULT_DB_PATH


@dataclass
class BudgetReport:
    project_human_id: str
    iteration_human_id: str | None
    cursor_invocations: int = 0
    total_duration_ms: int = 0
    per_role_counts: dict[str, int] = field(default_factory=dict)
    token_input: int | None = None
    token_output: int | None = None
    unreported_usage_count: int = 0
    approved_budget_tokens: int | None = None
    remaining_budget_tokens: int | None = None

    def format_lines(self) -> list[str]:
        lines = [
            f"project: {self.project_human_id}",
            f"iteration: {self.iteration_human_id or '(none)'}",
            f"cursor_invocations: {self.cursor_invocations}",
            f"duration_ms: {self.total_duration_ms}",
            "per_role_counts:",
        ]
        for role, count in sorted(self.per_role_counts.items()):
            lines.append(f"  {role}: {count}")
        lines.append(
            f"token_input: {self.token_input if self.token_input is not None else 'unknown'}"
        )
        lines.append(
            f"token_output: {self.token_output if self.token_output is not None else 'unknown'}"
        )
        lines.append(f"unreported_usage_count: {self.unreported_usage_count}")
        lines.append(
            f"approved_budget_tokens: "
            f"{self.approved_budget_tokens if self.approved_budget_tokens is not None else 'unknown'}"
        )
        lines.append(
            f"remaining_budget_tokens: "
            f"{self.remaining_budget_tokens if self.remaining_budget_tokens is not None else 'unknown'}"
        )
        return lines


def _parse_usage(usage_json: str | None) -> tuple[int | None, int | None, bool]:
    """Return (input, output, reported). Never invent values from text length."""
    if not usage_json:
        return None, None, False
    try:
        data = json.loads(usage_json)
    except json.JSONDecodeError:
        return None, None, False
    if not isinstance(data, dict):
        return None, None, False
    if data.get("status") == "unknown":
        return None, None, False
    # Accept only explicit numeric fields from Cursor.
    inp = data.get("input_tokens", data.get("prompt_tokens"))
    out = data.get("output_tokens", data.get("completion_tokens"))
    if isinstance(inp, int) and isinstance(out, int):
        return inp, out, True
    if isinstance(inp, int) and out is None:
        return inp, None, True
    if isinstance(out, int) and inp is None:
        return None, out, True
    return None, None, False


def build_budget_report(
    *,
    project_human_id: str,
    iteration_human_id: str | None = None,
    db_path: Path | str | None = None,
) -> BudgetReport:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    initialize_database(path)
    report = BudgetReport(
        project_human_id=project_human_id,
        iteration_human_id=iteration_human_id,
    )
    with connection(path) as conn:
        clauses = ["j.project_human_id = ?"]
        params: list[Any] = [project_human_id]
        if iteration_human_id:
            clauses.append("j.iteration_human_id = ?")
            params.append(iteration_human_id)
        where = " AND ".join(clauses)
        rows = conn.execute(
            f"""
            SELECT j.agent_role, r.duration_ms, r.usage_json
            FROM agent_runs r
            JOIN orchestration_jobs j ON j.id = r.job_id
            WHERE {where}
            """,
            params,
        ).fetchall()
        input_sum = 0
        output_sum = 0
        any_input = False
        any_output = False
        for row in rows:
            report.cursor_invocations += 1
            role = str(row["agent_role"])
            report.per_role_counts[role] = report.per_role_counts.get(role, 0) + 1
            if row["duration_ms"] is not None:
                report.total_duration_ms += int(row["duration_ms"])
            inp, out, reported = _parse_usage(row["usage_json"])
            if not reported:
                report.unreported_usage_count += 1
            else:
                if inp is not None:
                    input_sum += inp
                    any_input = True
                if out is not None:
                    output_sum += out
                    any_output = True
        report.token_input = input_sum if any_input else None
        report.token_output = output_sum if any_output else None

        try:
            sched = conn.execute(
                """
                SELECT approved_budget_tokens FROM project_schedules
                WHERE project_human_id = ?
                """,
                (project_human_id,),
            ).fetchone()
            if sched and sched["approved_budget_tokens"] is not None:
                report.approved_budget_tokens = int(sched["approved_budget_tokens"])
                used = (report.token_input or 0) + (report.token_output or 0)
                if report.token_input is not None or report.token_output is not None:
                    report.remaining_budget_tokens = (
                        report.approved_budget_tokens - used
                    )
        except Exception:
            pass
    return report
