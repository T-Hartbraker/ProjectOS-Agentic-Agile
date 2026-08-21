"""Deterministic ProjectOS health checks (doctor)."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from projectos.cursor_adapter import resolve_cursor_agent_bin
from projectos.db import connection, foreign_keys_enabled
from projectos.migrate import applied_versions, initialize_database, list_migration_files
from projectos.paths import (
    DEFAULT_DB_PATH,
    DEFAULT_REGISTRY_PATH,
    MIGRATIONS_DIR,
    PROJECTS_SCHEMA_PATH,
)
from projectos.registry import load_registry
from projectos.validation import validate_registry


@dataclass
class DoctorFinding:
    level: str  # ok | warn | fail
    code: str
    message: str


@dataclass
class DoctorReport:
    findings: list[DoctorFinding] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(f.level == "fail" for f in self.findings)

    @property
    def exit_code(self) -> int:
        return 1 if self.blocking else 0


def run_doctor(
    *,
    db_path: Path | str | None = None,
    registry_path: Path | str | None = None,
    projectctl_runner=None,
) -> DoctorReport:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    reg_path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    report = DoctorReport()

    # DB accessible + migrations
    try:
        initialize_database(path)
        with connection(path) as conn:
            if not foreign_keys_enabled(conn):
                report.findings.append(
                    DoctorFinding("fail", "db.foreign_keys", "foreign_keys not enabled")
                )
            else:
                report.findings.append(
                    DoctorFinding("ok", "db.accessible", f"DB ok at {path}")
                )
            applied = applied_versions(conn)
            expected = {p.name for p in list_migration_files(MIGRATIONS_DIR)}
            missing = expected - applied
            if missing:
                report.findings.append(
                    DoctorFinding(
                        "fail",
                        "db.migrations",
                        f"Missing migrations: {sorted(missing)}",
                    )
                )
            else:
                report.findings.append(
                    DoctorFinding("ok", "db.migrations", "Migrations current")
                )
            # Active leases sanity
            stale = conn.execute(
                """
                SELECT COUNT(*) FROM worker_leases l
                JOIN orchestration_jobs j ON j.id = l.job_id
                WHERE l.released_at IS NULL AND j.status NOT IN ('LEASED','RUNNING')
                """
            ).fetchone()[0]
            if int(stale) > 0:
                report.findings.append(
                    DoctorFinding(
                        "warn",
                        "leases.orphan",
                        f"{stale} active lease(s) on non-active jobs",
                    )
                )
            else:
                report.findings.append(
                    DoctorFinding("ok", "leases.sane", "Active leases sane")
                )
            conn.execute("SELECT * FROM scheduler_state WHERE id = 1").fetchone()
            report.findings.append(
                DoctorFinding("ok", "scheduler.readable", "Scheduler state readable")
            )
            daemon = conn.execute("SELECT * FROM daemon_state WHERE id = 1").fetchone()
            if daemon and daemon["status"] == "running" and daemon["pid"] is None:
                report.findings.append(
                    DoctorFinding(
                        "warn", "daemon.sane", "Daemon status running without PID"
                    )
                )
            else:
                report.findings.append(
                    DoctorFinding("ok", "daemon.sane", "Daemon state sane")
                )
    except Exception as exc:  # noqa: BLE001
        report.findings.append(DoctorFinding("fail", "db.accessible", str(exc)))
        return report

    # Registry + config
    if not PROJECTS_SCHEMA_PATH.is_file():
        report.findings.append(
            DoctorFinding("fail", "config.schema", "projects.schema.json missing")
        )
    else:
        report.findings.append(
            DoctorFinding("ok", "config.schema", "Registry schema readable")
        )
    try:
        registry = load_registry(reg_path)
        report.findings.append(
            DoctorFinding(
                "ok",
                "registry.valid",
                f"Registry loaded ({len(registry.projects)} projects)",
            )
        )
        for entry in registry.enabled_projects():
            if not entry.repository_root.exists():
                report.findings.append(
                    DoctorFinding(
                        "fail",
                        "registry.path",
                        f"{entry.project_human_id} root missing: {entry.repository_root}",
                    )
                )
        vreport = validate_registry(
            registry, projectctl_runner=projectctl_runner
        )
        for issue in vreport.issues:
            report.findings.append(
                DoctorFinding(
                    "fail",
                    "registry.identity",
                    f"{issue.project_human_id}: {issue.error}",
                )
            )
        if vreport.ok and registry.enabled_projects():
            report.findings.append(
                DoctorFinding("ok", "registry.identity", "No identity drift detected")
            )
    except Exception as exc:  # noqa: BLE001
        report.findings.append(DoctorFinding("fail", "registry.valid", str(exc)))

    # Cursor
    try:
        bin_path = resolve_cursor_agent_bin()
        report.findings.append(
            DoctorFinding("ok", "cursor.bin", f"Cursor Agent at {bin_path}")
        )
        try:
            completed = subprocess.run(
                [bin_path, "status"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if completed.returncode == 0:
                report.findings.append(
                    DoctorFinding("ok", "cursor.auth", "Cursor auth status readable")
                )
            else:
                report.findings.append(
                    DoctorFinding(
                        "warn",
                        "cursor.auth",
                        "Cursor auth status not confirmed",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            report.findings.append(
                DoctorFinding("warn", "cursor.auth", f"auth check skipped: {exc}")
            )
    except Exception as exc:  # noqa: BLE001
        report.findings.append(DoctorFinding("fail", "cursor.bin", str(exc)))

    # Git
    if shutil.which("git"):
        report.findings.append(DoctorFinding("ok", "git.bin", "git available"))
    else:
        report.findings.append(DoctorFinding("fail", "git.bin", "git not found"))

    return report
