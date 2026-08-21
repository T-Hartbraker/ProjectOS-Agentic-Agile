"""argparse CLI for projectos — Phase 2 operator surface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from projectos.budget import build_budget_report
from projectos.constants import DEFAULT_MAX_PARALLEL
from projectos.daemon import get_daemon_status, run_daemon, stop_daemon
from projectos.dispatch import run_dispatch
from projectos.doctor import run_doctor
from projectos.errors import ProjectOSError
from projectos.invalidate import reconcile_prj003_iter002_fat
from projectos.iteration import run_iteration
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.plan import run_plan
from projectos.recover import run_recovery
from projectos.registry import load_registry
from projectos.schedule import evaluate_due, list_schedules, upsert_schedule
from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.validation import validate_registry
from projectos.worker import run_once


def cmd_registry_list(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    if not registry.projects:
        print("(no projects registered)")
        return 0
    for entry in registry.projects:
        flag = "enabled" if entry.enabled else "disabled"
        print(f"{entry.project_human_id}  {flag}  {entry.repository_root}")
    return 0


def cmd_registry_show(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    entry = registry.get(args.project_human_id)
    if entry is None:
        print(
            f"error: project {args.project_human_id!r} is not in the registry",
            file=sys.stderr,
        )
        return 1
    print(f"project_human_id: {entry.project_human_id}")
    print(f"repository_root: {entry.repository_root}")
    print(f"enabled: {entry.enabled}")
    print(f"registry: {registry.path}")
    return 0


def cmd_registry_validate(args: argparse.Namespace) -> int:
    report = validate_registry(
        path=args.config,
        project_human_id=args.project_human_id,
    )
    for item in report.validated:
        print(
            f"OK  {item.entry.project_human_id}  "
            f"git_root={item.git_root}  "
            f"active={item.active_project_human_id}"
        )
    for issue in report.issues:
        label = issue.project_human_id or "(unknown)"
        print(f"FAIL  {label}  {issue.error}", file=sys.stderr)
    if not report.validated and not report.issues:
        print("(no enabled projects to validate)")
    return 0 if report.ok else 1


def cmd_worker(args: argparse.Namespace) -> int:
    result = run_once(
        db_path=args.db,
        registry_path=args.config,
        queue=args.queue,
        role=args.role,
        job_human_id=args.job_human_id,
        lease_seconds=args.lease_seconds,
        timeout_seconds=args.timeout,
    )
    if result.job_human_id:
        print(f"job: {result.job_human_id}")
    print(f"status: {result.status}")
    print(result.message)
    return int(result.exit_code)


def cmd_recover(args: argparse.Namespace) -> int:
    if getattr(args, "revalidate_blocked", False):
        from projectos.recover import revalidate_blocked_job

        if not args.job:
            print(
                "error: --revalidate-blocked requires --job <job_human_id>",
                file=sys.stderr,
            )
            return 1
        result = revalidate_blocked_job(
            job_human_id=args.job,
            db_path=args.db,
            registry_path=args.config,
        )
        print(f"job: {result.job_human_id}")
        print(f"status: {result.status}")
        print(result.message)
        if result.previous_error:
            print(f"previous_error: {result.previous_error}")
        if result.acceptance_criteria_count:
            print(f"acceptance_criteria: {result.acceptance_criteria_count}")
        if result.created_duplicate:
            print("warning: unexpected job row created during revalidation")
        return int(result.exit_code)

    if getattr(args, "reclaim_running", False):
        from projectos.db import connection
        from projectos.migrate import initialize_database
        from projectos.store import (
            get_job_by_human_id,
            promote_retry_wait_to_ready,
            reclaim_interrupted_running_job,
        )

        if not args.job:
            print(
                "error: --reclaim-running requires --job <job_human_id>",
                file=sys.stderr,
            )
            return 1
        initialize_database(args.db)
        with connection(args.db) as conn:
            before = get_job_by_human_id(conn, args.job)
            if before is None:
                print(f"error: job {args.job!r} not found", file=sys.stderr)
                return 1
            print(f"job: {before.human_id}")
            print(f"status_before: {before.status}")
            print(f"base_git_sha: {before.base_git_sha}")
            print(f"worktree_path: {before.worktree_path}")
            job = reclaim_interrupted_running_job(
                conn,
                args.job,
                reason=(
                    "governed reclaim after interrupted Cursor worker "
                    "(Ctrl+C / hang)"
                ),
            )
            if job.status == "RETRY_WAIT" and not args.no_promote:
                job = promote_retry_wait_to_ready(
                    conn,
                    job.id,
                    reason="recovery: reclaimed interrupted RUNNING -> READY",
                )
        print(f"status_after: {job.status}")
        print(f"attempt: {job.attempt}")
        print(f"last_error: {job.last_error}")
        return 0

    report = run_recovery(
        db_path=args.db,
        registry_path=args.config,
        promote_retry_wait=not args.no_promote,
    )
    print(f"expired_leases: {len(report.expired_lease_job_ids)}")
    print(f"promoted_ready: {len(report.promoted_ready)}")
    if report.promoted_ready:
        print(f"  jobs: {', '.join(report.promoted_ready)}")
    print(f"blocked: {len(report.blocked)}")
    if report.blocked:
        print(f"  jobs: {', '.join(report.blocked)}")
    print(f"identity_checks: {len(report.identity_checks)}")
    for check in report.identity_checks:
        mark = "OK" if check.ok else "FAIL"
        detail = check.error or check.repository_root or ""
        print(f"  {mark}  {check.project_human_id}  {detail}")
    print(f"worktree_actions: {len(report.worktree_actions)}")
    for action in report.worktree_actions:
        print(f"  {action.action}  {action.job_human_id}  {action.message}")
    if report.unknown_worktrees_ignored:
        print(f"unknown_worktrees_ignored: {len(report.unknown_worktrees_ignored)}")
        for path in report.unknown_worktrees_ignored:
            print(f"  ignored  {path}")
    for message in report.messages:
        print(message)
    return 0 if report.ok else 1


def cmd_cursor_smoke(args: argparse.Namespace) -> int:
    from projectos.cursor_adapter import run_cursor_smoke_test

    result = run_cursor_smoke_test(
        workspace=args.workspace,
        timeout_seconds=args.timeout,
        agent_bin=args.agent_bin,
    )
    cursor = result.cursor
    print(f"returncode: {cursor.returncode}")
    print(f"timed_out: {cursor.timed_out}")
    print(f"cancelled: {cursor.cancelled}")
    print(f"duration_ms: {cursor.duration_ms}")
    print(f"command: {' '.join(cursor.command[:-1])} <prompt>")
    print(f"stdout_ref: {cursor.stdout_ref}")
    print(f"stderr_ref: {cursor.stderr_ref}")
    print(f"output_ref: {cursor.output_ref}")
    if result.parsed_ref:
        print(f"parsed_ref: {result.parsed_ref}")
    print(f"result_text: {result.result_text!r}")
    print(f"reason: {result.reason}")
    print("--- stdout ---")
    print(cursor.stdout.strip() or "(empty)")
    print("--- stderr ---")
    print(cursor.stderr.strip() or "(empty)")
    print(f"smoke_ok: {result.smoke_ok}")
    return int(result.exit_code)


def cmd_plan(args: argparse.Namespace) -> int:
    result = run_plan(
        project_human_id=args.project,
        dry_run=args.dry_run,
        iteration_human_id=args.iteration,
        db_path=args.db,
        registry_path=args.config,
    )
    print(f"status: {result.status}")
    print(f"project: {result.project_human_id}")
    print(f"dry_run: {result.dry_run}")
    if result.plan_source:
        print(f"plan_source: {result.plan_source}")
    if result.jobs_created:
        print(f"jobs_created: {', '.join(result.jobs_created)}")
    if result.plan and isinstance(result.plan.get("jobs"), list):
        print("proposed_jobs:")
        for job in result.plan["jobs"]:
            if not isinstance(job, dict):
                continue
            deps = job.get("depends_on") or []
            dep_txt = ",".join(str(d) for d in deps) if deps else "-"
            print(
                f"  - {job.get('human_id')}  queue={job.get('queue')}  "
                f"role={job.get('agent_role')}  depends_on={dep_txt}"
            )
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
    return 0 if result.ok else 1


def cmd_dispatch(args: argparse.Namespace) -> int:
    result = run_dispatch(
        once=args.once or not args.until_idle,
        until_idle=args.until_idle,
        max_parallel=args.max_parallel,
        db_path=args.db,
        registry_path=args.config,
        lease_seconds=args.lease_seconds,
        timeout_seconds=args.timeout,
    )
    print(result.message)
    for item in result.completed:
        print(f"  {item.job_human_id or '-'}  {item.status}  {item.message}")
    return int(result.exit_code)


def cmd_budget(args: argparse.Namespace) -> int:
    report = build_budget_report(
        project_human_id=args.project,
        iteration_human_id=args.iteration,
        db_path=args.db,
    )
    for line in report.format_lines():
        print(line)
    return 0


def cmd_iteration_run(args: argparse.Namespace) -> int:
    result = run_iteration(
        project_human_id=args.project,
        iteration_human_id=args.iteration,
        dry_run=args.dry_run,
        max_parallel=args.max_parallel,
        db_path=args.db,
        registry_path=args.config,
    )
    print(f"project: {result.project_human_id}")
    print(f"iteration: {result.iteration_human_id}")
    print(f"status: {result.status}")
    print(f"checkpoints: {', '.join(result.checkpoints)}")
    print(result.message)
    return int(result.exit_code)


def cmd_schedule_show(args: argparse.Namespace) -> int:
    initialize_database(args.db)
    with connection(args.db) as conn:
        entries = list_schedules(conn)
    if not entries:
        print("(no schedules configured)")
        return 0
    for e in entries:
        flag = "enabled" if e.enabled else "disabled"
        print(
            f"{e.project_human_id}  {flag}  {e.cadence}  {e.timezone}  {e.local_time}"
        )
    return 0


def cmd_schedule_due(args: argparse.Namespace) -> int:
    report = evaluate_due(db_path=args.db, registry_path=args.config)
    for item in report.due:
        mark = "TRIGGERED" if item.triggered else "skip"
        print(f"{mark}  {item.project_human_id}  {item.window_key}  {item.reason}")
    return 0


def cmd_schedule_set(args: argparse.Namespace) -> int:
    initialize_database(args.db)
    with connection(args.db) as conn:
        upsert_schedule(
            conn,
            project_human_id=args.project,
            enabled=not args.disabled,
            timezone=args.timezone,
            cadence=args.cadence,
            local_time=args.local_time,
            approved_budget_tokens=args.budget_tokens,
        )
    print(f"schedule updated for {args.project}")
    return 0


def cmd_daemon_run(args: argparse.Namespace) -> int:
    return run_daemon(
        db_path=args.db,
        registry_path=args.config,
        poll_seconds=args.poll_seconds,
        max_loops=args.max_loops,
    )


def cmd_daemon_status(args: argparse.Namespace) -> int:
    status = get_daemon_status(args.db)
    print(f"status: {status.status}")
    print(f"pid: {status.pid}")
    print(f"started_at: {status.started_at}")
    print(f"heartbeat_at: {status.heartbeat_at}")
    print(f"lock_path: {status.lock_path}")
    print(f"last_error: {status.last_error}")
    return 0


def cmd_daemon_stop(args: argparse.Namespace) -> int:
    return stop_daemon(args.db)


def cmd_doctor(args: argparse.Namespace) -> int:
    report = run_doctor(db_path=args.db, registry_path=args.config)
    for finding in report.findings:
        print(f"{finding.level.upper():4}  {finding.code}  {finding.message}")
    return int(report.exit_code)


def cmd_fat_reconcile(args: argparse.Namespace) -> int:
    """Governed FAT reconciliation — does not dispatch workers."""
    if args.project != "PRJ-003" or args.iteration != "ITER-002":
        print(
            "error: only PRJ-003 / ITER-002 FAT reconcile is implemented",
            file=sys.stderr,
        )
        return 1
    result = reconcile_prj003_iter002_fat(
        db_path=args.db,
        registry_path=args.config,
        ensure_work_items=not args.skip_work_items,
    )
    print(f"project: {result.project_human_id}")
    print(f"iteration: {result.iteration_human_id}")
    for key, hid in result.work_items.items():
        print(f"work_item: {key}={hid}")
    for inv in result.invalidations:
        print(
            f"invalidated: {inv.delivery_human_id} -> rework={inv.rework_human_id} "
            f"assurance_cancelled={len(inv.assurance_cancelled)}"
        )
        if inv.error:
            print(f"  error: {inv.error}", file=sys.stderr)
    print(f"integration_rewired: {result.integration_rewired}")
    for msg in result.messages:
        print(msg)
    print("NOTE: corrected delivery jobs were NOT dispatched.")
    return 0 if result.ok else 1


def _add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to projectos.db (default: {DEFAULT_DB_PATH})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="projectos",
        description="ProjectOS multi-project orchestration CLI",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_REGISTRY_PATH,
        help=f"Path to projects.json (default: {DEFAULT_REGISTRY_PATH})",
    )
    sub = parser.add_subparsers(dest="command")

    # registry
    p_reg = sub.add_parser("registry", help="Project registry commands")
    reg_sub = p_reg.add_subparsers(dest="registry_command", required=True)
    p_list = reg_sub.add_parser("list", help="List registered projects")
    p_list.set_defaults(func=cmd_registry_list)
    p_show = reg_sub.add_parser("show", help="Show one registered project")
    p_show.add_argument("project_human_id")
    p_show.set_defaults(func=cmd_registry_show)
    p_val = reg_sub.add_parser("validate", help="Validate registered repositories")
    p_val.add_argument("project_human_id", nargs="?", default=None)
    p_val.set_defaults(func=cmd_registry_validate)

    # plan
    p_plan = sub.add_parser("plan", help="PM planning into durable orchestration jobs")
    p_plan.add_argument("--project", required=True, help="Project human ID")
    p_plan.add_argument("--iteration", default=None)
    p_plan.add_argument("--dry-run", action="store_true")
    _add_db_arg(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    # worker
    p_worker = sub.add_parser("worker", help="Execute one orchestration job")
    p_worker.add_argument("--once", action="store_true")
    p_worker.add_argument("--queue", default=None)
    p_worker.add_argument("--role", default=None)
    p_worker.add_argument("--job", default=None, dest="job_human_id")
    p_worker.add_argument("--lease-seconds", type=int, default=900)
    p_worker.add_argument("--timeout", type=float, default=1800.0)
    _add_db_arg(p_worker)
    p_worker.set_defaults(func=cmd_worker)

    # dispatch
    p_disp = sub.add_parser("dispatch", help="Bounded parallel dispatch of READY jobs")
    mode = p_disp.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--until-idle", action="store_true")
    p_disp.add_argument("--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL)
    p_disp.add_argument("--lease-seconds", type=int, default=900)
    p_disp.add_argument("--timeout", type=float, default=1800.0)
    _add_db_arg(p_disp)
    p_disp.set_defaults(func=cmd_dispatch)

    # recover
    p_recover = sub.add_parser("recover", help="Recover leases, identity, worktrees")
    _add_db_arg(p_recover)
    p_recover.add_argument("--no-promote", action="store_true")
    p_recover.add_argument(
        "--revalidate-blocked",
        action="store_true",
        help="Revalidate a BLOCKED job against persisted work-item identity",
    )
    p_recover.add_argument(
        "--reclaim-running",
        action="store_true",
        help="Governed reclaim of an interrupted RUNNING/LEASED job",
    )
    p_recover.add_argument(
        "--job",
        default=None,
        dest="job",
        help="Job human id (for --revalidate-blocked / --reclaim-running)",
    )
    p_recover.set_defaults(func=cmd_recover)

    # cursor diagnostics
    p_cursor = sub.add_parser("cursor", help="Cursor adapter diagnostics")
    cursor_sub = p_cursor.add_subparsers(dest="cursor_command", required=True)
    p_smoke = cursor_sub.add_parser(
        "smoke",
        help="Unattended headless smoke test (operator diagnostic)",
    )
    p_smoke.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Workspace / worktree path for --workspace",
    )
    p_smoke.add_argument("--timeout", type=float, default=120.0)
    p_smoke.add_argument(
        "--agent-bin",
        default=None,
        help="Optional path to agent / agent.cmd",
    )
    p_smoke.set_defaults(func=cmd_cursor_smoke)

    # budget
    p_budget = sub.add_parser("budget", help="Run accounting / budget report")
    p_budget.add_argument("--project", required=True)
    p_budget.add_argument("--iteration", default=None)
    _add_db_arg(p_budget)
    p_budget.set_defaults(func=cmd_budget)

    # iteration
    p_iter = sub.add_parser("iteration", help="Iteration conductor")
    iter_sub = p_iter.add_subparsers(dest="iteration_command", required=True)
    p_iter_run = iter_sub.add_parser("run", help="Run iteration conductor")
    p_iter_run.add_argument("--project", required=True)
    p_iter_run.add_argument("--iteration", default=None)
    p_iter_run.add_argument("--dry-run", action="store_true")
    p_iter_run.add_argument("--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL)
    _add_db_arg(p_iter_run)
    p_iter_run.set_defaults(func=cmd_iteration_run)

    # schedule
    p_sched = sub.add_parser("schedule", help="Per-project schedule controls")
    sched_sub = p_sched.add_subparsers(dest="schedule_command", required=True)
    p_sched_show = sched_sub.add_parser("show", help="Show schedules")
    _add_db_arg(p_sched_show)
    p_sched_show.set_defaults(func=cmd_schedule_show)
    p_sched_due = sched_sub.add_parser("due", help="Evaluate due schedules")
    _add_db_arg(p_sched_due)
    p_sched_due.set_defaults(func=cmd_schedule_due)
    p_sched_set = sched_sub.add_parser("set", help="Upsert a project schedule")
    p_sched_set.add_argument("--project", required=True)
    p_sched_set.add_argument("--timezone", default="UTC")
    p_sched_set.add_argument("--cadence", default="daily")
    p_sched_set.add_argument("--local-time", default="09:00")
    p_sched_set.add_argument("--budget-tokens", type=int, default=None)
    p_sched_set.add_argument("--disabled", action="store_true")
    _add_db_arg(p_sched_set)
    p_sched_set.set_defaults(func=cmd_schedule_set)

    # daemon
    p_daemon = sub.add_parser("daemon", help="Long-running orchestration daemon")
    daemon_sub = p_daemon.add_subparsers(dest="daemon_command", required=True)
    p_daemon_run = daemon_sub.add_parser("run", help="Run daemon loop")
    p_daemon_run.add_argument("--poll-seconds", type=float, default=5.0)
    p_daemon_run.add_argument("--max-loops", type=int, default=None)
    _add_db_arg(p_daemon_run)
    p_daemon_run.set_defaults(func=cmd_daemon_run)
    p_daemon_status = daemon_sub.add_parser("status", help="Show daemon status")
    _add_db_arg(p_daemon_status)
    p_daemon_status.set_defaults(func=cmd_daemon_status)
    p_daemon_stop = daemon_sub.add_parser("stop", help="Stop daemon")
    _add_db_arg(p_daemon_stop)
    p_daemon_stop.set_defaults(func=cmd_daemon_stop)

    # doctor
    p_doctor = sub.add_parser("doctor", help="Deterministic health check")
    _add_db_arg(p_doctor)
    p_doctor.set_defaults(func=cmd_doctor)

    p_fat = sub.add_parser(
        "fat",
        help="FAT evidence reconciliation (no dispatch)",
    )
    fat_sub = p_fat.add_subparsers(dest="fat_command", required=True)
    p_fat_rec = fat_sub.add_parser(
        "reconcile",
        help="Invalidate no-op delivery candidates and create rework jobs",
    )
    p_fat_rec.add_argument("--project", required=True)
    p_fat_rec.add_argument("--iteration", required=True)
    p_fat_rec.add_argument(
        "--skip-work-items",
        action="store_true",
        help="Do not create/ensure projectctl stories (tests only)",
    )
    _add_db_arg(p_fat_rec)
    p_fat_rec.set_defaults(func=cmd_fat_reconcile)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return int(code) if isinstance(code, int) else 1

    if not getattr(args, "command", None) or not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except ProjectOSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
