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

    _add_slack_findings(report)
    _add_openai_findings(report)
    _add_cockpit_findings(report, path)
    return report


def _add_cockpit_findings(report: DoctorReport, db_path: Path) -> None:
    try:
        from projectos.event_dispatcher import outbox_diagnostics

        with connection(db_path) as conn:
            handoffs = conn.execute("SELECT COUNT(*) FROM sponsor_handoffs").fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
            domain_events = conn.execute("SELECT COUNT(*) FROM projectos_events").fetchone()[0]
            legacy_events = conn.execute("SELECT COUNT(*) FROM agent_activity_events").fetchone()[0]
            outbox = outbox_diagnostics(conn)
            pending = int(outbox.get("pending", 0))
            dead = int(outbox.get("dead", 0))
            legacy_pending = conn.execute(
                "SELECT COUNT(*) FROM slack_activity_outbox WHERE status = 'pending'"
            ).fetchone()[0]
        report.findings.append(
            DoctorFinding(
                "ok",
                "cockpit.handoffs",
                f"{int(handoffs)} handoff(s) recorded",
            )
        )
        report.findings.append(
            DoctorFinding("ok", "cockpit.runs", f"{int(runs)} execution run(s)")
        )
        report.findings.append(
            DoctorFinding(
                "ok",
                "cockpit.domain_events",
                f"{int(domain_events)} canonical event(s); {int(legacy_events)} legacy audit event(s)",
            )
        )
        if int(legacy_pending):
            report.findings.append(
                DoctorFinding(
                    "warn",
                    "cockpit.legacy_outbox",
                    f"{int(legacy_pending)} legacy slack_activity_outbox row(s) pending (retired path)",
                )
            )
        if dead:
            report.findings.append(
                DoctorFinding(
                    "warn",
                    "cockpit.outbox",
                    f"{dead} dead message(s); {pending} pending canonical projection(s)",
                )
            )
        else:
            report.findings.append(
                DoctorFinding(
                    "ok",
                    "cockpit.outbox",
                    f"{pending} pending canonical Slack projection(s)",
                )
            )
    except Exception as exc:  # noqa: BLE001
        report.findings.append(
            DoctorFinding("warn", "cockpit.schema", f"Enterprise cockpit tables unavailable: {exc}")
        )


def _add_openai_findings(report: DoctorReport) -> None:
    from projectos.openai_settings import read_openai_settings
    from projectos.openai_tokens import contains_secret

    settings = read_openai_settings()
    if contains_secret(str(settings)):
        report.findings.append(DoctorFinding("error", "openai.secrets", "status leaked a secret"))
        return
    if settings["api_key_configured"]:
        level = "ok" if settings.get("api_key_source") != "none" else "warn"
        label = "configured" if settings["api_key_source"] != "none" else "missing"
        report.findings.append(DoctorFinding(level, "openai.api_key", label))
    else:
        report.findings.append(
            DoctorFinding("warn", "openai.api_key", "not configured (Settings or PROJECTOS_OPENAI_API_KEY)")
        )
    report.findings.append(
        DoctorFinding("ok", "openai.api_key_source", str(settings.get("api_key_source") or "none"))
    )
    report.findings.append(DoctorFinding("ok", "openai.model", settings["model"]))
    last_status = settings.get("last_test_status")
    if last_status == "success":
        report.findings.append(DoctorFinding("ok", "openai.connection", "success"))
    elif last_status == "failed":
        report.findings.append(
            DoctorFinding("warn", "openai.connection", settings.get("last_error") or "failed")
        )
    else:
        report.findings.append(DoctorFinding("ok", "openai.connection", "not tested"))


def _add_slack_findings(report: DoctorReport) -> None:
    from projectos.operator import load_operator_config
    from projectos.slack_state import public_connection
    from projectos.slack_tokens import APP_PREFIX, BOT_PREFIX, token_report

    cfg = load_operator_config()
    tokens = token_report()
    from projectos.runtime_deps import http_deps_missing

    missing = http_deps_missing()
    if missing:
        report.findings.append(
            DoctorFinding(
                "error",
                "slack.runtime_deps",
                "missing: " + ", ".join(missing) + " (restart ProjectOS after install)",
            )
        )
    else:
        report.findings.append(DoctorFinding("ok", "slack.runtime_deps", "installed"))
    report.findings.append(DoctorFinding("ok", "slack.mode", "Socket Mode"))
    if tokens["app_token_present"]:
        level = "ok" if tokens["app_token_valid_prefix"] else "warn"
        detail = tokens["app_token"]
        if not tokens["app_token_valid_prefix"]:
            detail = f"{tokens['app_token']} (expected {APP_PREFIX} prefix)"
        report.findings.append(DoctorFinding(level, "slack.app_token", detail))
    else:
        report.findings.append(DoctorFinding("warn", "slack.app_token", "missing"))
    if tokens["bot_token_present"]:
        level = "ok" if tokens["bot_token_valid_prefix"] else "warn"
        detail = tokens["bot_token"]
        if not tokens["bot_token_valid_prefix"]:
            detail = f"{tokens['bot_token']} (expected {BOT_PREFIX} prefix)"
        report.findings.append(DoctorFinding(level, "slack.bot_token", detail))
    else:
        report.findings.append(DoctorFinding("warn", "slack.bot_token", "missing"))
    info = public_connection(
        enabled=cfg.slack_enabled,
        tokens_ready=bool(tokens["app_token_present"] and tokens["bot_token_present"]),
    )
    status = str(info.get("status") or "disconnected")
    updated = str(info.get("updated_at") or "").strip()
    stale_note = f" (persisted {updated})" if updated and status in {"error", "disconnected"} else ""
    if status == "connected":
        report.findings.append(DoctorFinding("ok", "slack.connection", "connected"))
    else:
        report.findings.append(
            DoctorFinding("warn", "slack.connection", status.replace("_", " ") + stale_note)
        )
    if tokens["bot_token_present"] and tokens["bot_token_valid_prefix"]:
        report.findings.append(
            DoctorFinding("ok", "slack.auth", "bot token present (live auth.test not run)")
        )
    else:
        report.findings.append(DoctorFinding("warn", "slack.auth", "not configured"))

