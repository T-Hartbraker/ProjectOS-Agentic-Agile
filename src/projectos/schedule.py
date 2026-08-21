"""Per-project schedule evaluation (timezone-aware, idempotent)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from projectos.clock import Clock, system_utc_now
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.registry import load_registry
from projectos.store import utc_now_iso
from projectos.validation import validate_registry_entry


@dataclass
class ScheduleEntry:
    project_human_id: str
    enabled: bool
    timezone: str
    cadence: str
    local_time: str
    approved_budget_tokens: int | None


@dataclass
class DueResult:
    project_human_id: str
    window_key: str
    triggered: bool
    reason: str


@dataclass
class ScheduleReport:
    entries: list[ScheduleEntry] = field(default_factory=list)
    due: list[DueResult] = field(default_factory=list)


def upsert_schedule(
    conn,
    *,
    project_human_id: str,
    enabled: bool = True,
    timezone: str = "UTC",
    cadence: str = "daily",
    local_time: str = "09:00",
    approved_budget_tokens: int | None = None,
) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO project_schedules (
            project_human_id, enabled, timezone, cadence, local_time,
            approved_budget_tokens, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_human_id) DO UPDATE SET
            enabled=excluded.enabled,
            timezone=excluded.timezone,
            cadence=excluded.cadence,
            local_time=excluded.local_time,
            approved_budget_tokens=excluded.approved_budget_tokens,
            updated_at=excluded.updated_at
        """,
        (
            project_human_id,
            1 if enabled else 0,
            timezone,
            cadence,
            local_time,
            approved_budget_tokens,
            now,
        ),
    )


def list_schedules(conn) -> list[ScheduleEntry]:
    rows = conn.execute(
        "SELECT * FROM project_schedules ORDER BY project_human_id"
    ).fetchall()
    return [
        ScheduleEntry(
            project_human_id=str(r["project_human_id"]),
            enabled=bool(r["enabled"]),
            timezone=str(r["timezone"]),
            cadence=str(r["cadence"]),
            local_time=str(r["local_time"]),
            approved_budget_tokens=(
                int(r["approved_budget_tokens"])
                if r["approved_budget_tokens"] is not None
                else None
            ),
        )
        for r in rows
    ]


def window_key_for(now_local: datetime, cadence: str) -> str:
    if cadence == "daily":
        return now_local.strftime("%Y-%m-%d")
    if cadence == "weekly":
        iso = now_local.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return now_local.strftime("%Y-%m-%d")


def is_due_now(entry: ScheduleEntry, now_utc: datetime) -> tuple[bool, str]:
    if not entry.enabled:
        return False, entry.project_human_id
    if entry.timezone.upper() in {"UTC", "GMT"}:
        local = now_utc.astimezone(timezone.utc)
    else:
        tz = ZoneInfo(entry.timezone)
        local = now_utc.astimezone(tz)
    hour, minute = [int(x) for x in entry.local_time.split(":", 1)]
    # Due once the local time has reached the configured clock time for the window.
    reached = (local.hour, local.minute) >= (hour, minute)
    key = window_key_for(local, entry.cadence)
    return reached, key


def evaluate_due(
    *,
    db_path: Path | str | None = None,
    registry_path: Path | str | None = None,
    clock: Clock | None = None,
    projectctl_runner=None,
    create_iteration_run: bool = True,
) -> ScheduleReport:
    """Evaluate schedules; trigger at most one run per project/window."""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    reg_path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    initialize_database(path)
    now = (clock or system_utc_now)()
    report = ScheduleReport()
    registry = load_registry(reg_path)

    with connection(path) as conn:
        entries = list_schedules(conn)
        report.entries = entries
        conn.execute(
            """
            UPDATE scheduler_state
            SET last_due_eval_at = ?, updated_at = ?
            WHERE id = 1
            """,
            (utc_now_iso(), utc_now_iso()),
        )
        for entry in entries:
            if not entry.enabled:
                report.due.append(
                    DueResult(entry.project_human_id, "", False, "disabled")
                )
                continue
            reg_entry = registry.get(entry.project_human_id)
            if reg_entry is None or not reg_entry.enabled:
                report.due.append(
                    DueResult(
                        entry.project_human_id, "", False, "not registered/enabled"
                    )
                )
                continue
            try:
                validate_registry_entry(
                    reg_entry, projectctl_runner=projectctl_runner
                )
            except Exception as exc:  # noqa: BLE001
                report.due.append(
                    DueResult(
                        entry.project_human_id, "", False, f"validation failed: {exc}"
                    )
                )
                continue

            reached, key = is_due_now(entry, now)
            if not reached:
                report.due.append(
                    DueResult(entry.project_human_id, key, False, "not yet due")
                )
                continue
            existing = conn.execute(
                """
                SELECT id FROM schedule_triggers
                WHERE project_human_id = ? AND window_key = ?
                """,
                (entry.project_human_id, key),
            ).fetchone()
            if existing:
                report.due.append(
                    DueResult(
                        entry.project_human_id,
                        key,
                        False,
                        "already triggered this window",
                    )
                )
                continue

            iteration_run_id = None
            if create_iteration_run:
                cur = conn.execute(
                    """
                    INSERT INTO iteration_runs (
                        project_human_id, iteration_human_id, status, notes
                    ) VALUES (?, ?, 'READY', ?)
                    """,
                    (
                        entry.project_human_id,
                        f"SCHED-{key}",
                        f"schedule trigger {key}",
                    ),
                )
                iteration_run_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO schedule_triggers (
                    project_human_id, window_key, triggered_at, iteration_run_id
                ) VALUES (?, ?, ?, ?)
                """,
                (entry.project_human_id, key, utc_now_iso(), iteration_run_id),
            )
            report.due.append(
                DueResult(entry.project_human_id, key, True, "triggered")
            )
            # Scheduler never manufactures a release.
    return report
