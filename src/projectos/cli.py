"""argparse CLI for projectos — Phase 2 operator surface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from projectos.constants import DEFAULT_MAX_PARALLEL
from projectos.errors import ProjectOSError
from projectos.operator import operator_health, start_operator, stop_operator
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.services import (
    ApprovalService,
    DaemonService,
    DispatchService,
    IterationService,
    MemoryAdminService,
    PlanService,
    RecoverService,
    RegistryService,
    ReportingService,
    SlackBindingService,
    ServiceContext,
    WorkerService,
)


def _ctx(args: argparse.Namespace) -> ServiceContext:
    return ServiceContext.from_cli_args(args)


def cmd_registry_list(args: argparse.Namespace) -> int:
    registry = RegistryService(_ctx(args)).load()
    if not registry.projects:
        print("(no projects registered)")
        return 0
    for entry in registry.projects:
        flag = "enabled" if entry.enabled else "disabled"
        print(f"{entry.project_human_id}  {flag}  {entry.repository_root}")
    return 0


def cmd_registry_show(args: argparse.Namespace) -> int:
    svc = RegistryService(_ctx(args))
    entry = svc.show(args.project_human_id)
    print(f"project_human_id: {entry.project_human_id}")
    print(f"repository_root: {entry.repository_root}")
    print(f"enabled: {entry.enabled}")
    print(f"registry: {svc.load().path}")
    return 0


def cmd_registry_validate(args: argparse.Namespace) -> int:
    report = RegistryService(_ctx(args)).validate(args.project_human_id)
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


def _print_onboarding(result) -> None:
    print(f"action: {result.action}")
    print(f"project_human_id: {result.entry.project_human_id}")
    print(f"repository_root: {result.entry.repository_root}")
    print(f"enabled: {result.entry.enabled}")
    print(f"git_root: {result.git_root}")
    if result.identity.project_name:
        print(f"project_name: {result.identity.project_name}")
    print(f"project_control_dir: {result.project_control_dir}")


def cmd_registry_register(args: argparse.Namespace) -> int:
    result = RegistryService(_ctx(args)).register(args.repository_path)
    _print_onboarding(result)
    return 0


def cmd_registry_update(args: argparse.Namespace) -> int:
    result = RegistryService(_ctx(args)).update(
        args.project_human_id,
        repository_path=args.path,
    )
    _print_onboarding(result)
    return 0


def cmd_registry_disable(args: argparse.Namespace) -> int:
    result = RegistryService(_ctx(args)).disable(args.project_human_id)
    _print_onboarding(result)
    return 0


def _print_operator_health(snapshot: dict) -> None:
    print(f"operator: {snapshot['status']}")
    print(f"ready: {snapshot['ready']}")
    if snapshot.get("notice"):
        print(f"notice: {snapshot['notice']}")
    for item in snapshot.get("components") or []:
        pid = f"  pid={item['pid']}" if item.get("pid") else ""
        print(f"  {item['name']}: {item['status']}  {item['detail']}{pid}")


def cmd_start(args: argparse.Namespace) -> int:
    snapshot = start_operator(
        _ctx(args),
        wait=bool(getattr(args, "wait", False)),
    )
    _print_operator_health(snapshot)
    # Children need a few seconds to bind ports. The launcher waits for
    # dashboard/API health. Do not treat "not ready yet" as a failed start.
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    snapshot = stop_operator(_ctx(args))
    _print_operator_health(snapshot)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    snapshot = operator_health(_ctx(args))
    _print_operator_health(snapshot)
    return 0 if snapshot.get("ready") else 1


def cmd_dashboard_build(args: argparse.Namespace) -> int:
    from projectos.dashboard_build import built_dashboard_contains, ensure_dashboard_built

    force = bool(getattr(args, "force", False))
    try:
        ensure_dashboard_built(force=force)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    marker = "settings-link"
    if not built_dashboard_contains(marker):
        print(
            f"error: dashboard build completed but served bundle is missing {marker!r}",
            file=sys.stderr,
        )
        return 1
    from projectos.paths import dashboard_index

    print(f"dashboard: built at {dashboard_index()}")
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "error: HTTP extras required. Install with: pip install 'agentic-projectos[http]'",
            file=sys.stderr,
        )
        return 1
    from projectos.dashboard_build import ensure_dashboard_built
    from projectos.http import create_app

    try:
        ensure_dashboard_built()
    except RuntimeError as exc:
        print(f"warning: dashboard build failed: {exc}", file=sys.stderr)
    app = create_app(registry_path=args.config, db_path=getattr(args, "db", None))
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    result = WorkerService(_ctx(args)).run_once(
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
    svc = RecoverService(_ctx(args))
    if getattr(args, "salvage_candidate", False):
        result = svc.salvage(args.job)
        print(f"job: {result.job_human_id or args.job}")
        print(f"status: {result.status}")
        if result.outcome:
            print(f"outcome: {result.outcome}")
        print(f"attempt: {result.attempt}")
        if result.base_git_sha:
            print(f"base_git_sha: {result.base_git_sha}")
        if result.candidate_git_sha:
            print(f"candidate_git_sha: {result.candidate_git_sha}")
        if result.worktree_path:
            print(f"worktree_path: {result.worktree_path}")
        if result.assurance_job_ids:
            print(f"assurance_jobs: {', '.join(result.assurance_job_ids)}")
        print(result.message)
        return int(result.exit_code)

    if getattr(args, "reconcile_release", False):
        result = svc.reconcile_release(args.job)
        print(f"job: {result.job_human_id or args.job}")
        print(f"status: {result.status}")
        if result.outcome:
            print(f"outcome: {result.outcome}")
        print(f"attempt: {result.attempt}")
        if result.candidate_git_sha:
            print(f"candidate_git_sha: {result.candidate_git_sha}")
        if result.successor_job_human_id:
            print(f"successor_job: {result.successor_job_human_id}")
        if result.successor_status:
            print(f"successor_status: {result.successor_status}")
        if result.source_candidate_sha:
            print(f"source_candidate_sha: {result.source_candidate_sha}")
        if result.integration_job_human_id:
            print(f"integration_job: {result.integration_job_human_id}")
        if result.already_reconciled:
            print("already_reconciled: true")
        print(result.message)
        return int(result.exit_code)

    if getattr(args, "revalidate_blocked", False):
        result = svc.revalidate_blocked(args.job)
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
        result = svc.reclaim_running(args.job, promote=not args.no_promote)
        print(f"job: {result.job_human_id}")
        print(f"status_before: {result.status_before}")
        print(f"base_git_sha: {result.base_git_sha}")
        print(f"worktree_path: {result.worktree_path}")
        print(f"status_after: {result.status_after}")
        print(f"attempt: {result.attempt}")
        print(f"last_error: {result.last_error}")
        return 0

    report = svc.run(promote_retry_wait=not args.no_promote)
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
    result = PlanService(_ctx(args)).run(
        args.project,
        dry_run=args.dry_run,
        iteration_human_id=args.iteration,
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
    result = DispatchService(_ctx(args)).run(
        once=args.once or not args.until_idle,
        until_idle=args.until_idle,
        max_parallel=args.max_parallel,
        lease_seconds=args.lease_seconds,
        timeout_seconds=args.timeout,
    )
    print(result.message)
    for item in result.completed:
        print(f"  {item.job_human_id or '-'}  {item.status}  {item.message}")
    return int(result.exit_code)


def cmd_budget(args: argparse.Namespace) -> int:
    report = ReportingService(_ctx(args)).budget(
        args.project,
        iteration_human_id=args.iteration,
    )
    for line in report.format_lines():
        print(line)
    return 0


def cmd_iteration_run(args: argparse.Namespace) -> int:
    result = IterationService(_ctx(args)).run(
        args.project,
        iteration_human_id=args.iteration,
        dry_run=args.dry_run,
        max_parallel=args.max_parallel,
    )
    print(f"project: {result.project_human_id}")
    print(f"iteration: {result.iteration_human_id}")
    print(f"status: {result.status}")
    print(f"checkpoints: {', '.join(result.checkpoints)}")
    print(result.message)
    return int(result.exit_code)


def cmd_schedule_show(args: argparse.Namespace) -> int:
    entries = ReportingService(_ctx(args)).list_schedules()
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
    report = ReportingService(_ctx(args)).evaluate_due()
    for item in report.due:
        mark = "TRIGGERED" if item.triggered else "skip"
        print(f"{mark}  {item.project_human_id}  {item.window_key}  {item.reason}")
    return 0


def cmd_schedule_set(args: argparse.Namespace) -> int:
    ReportingService(_ctx(args)).upsert_schedule(
        args.project,
        enabled=not args.disabled,
        timezone=args.timezone,
        cadence=args.cadence,
        local_time=args.local_time,
        approved_budget_tokens=args.budget_tokens,
    )
    print(f"schedule updated for {args.project}")
    return 0


def cmd_daemon_run(args: argparse.Namespace) -> int:
    return DaemonService(_ctx(args)).run(
        poll_seconds=args.poll_seconds,
        max_loops=args.max_loops,
    )


def cmd_daemon_status(args: argparse.Namespace) -> int:
    status = DaemonService(_ctx(args)).status()
    print(f"status: {status.status}")
    print(f"pid: {status.pid}")
    print(f"started_at: {status.started_at}")
    print(f"heartbeat_at: {status.heartbeat_at}")
    print(f"lock_path: {status.lock_path}")
    print(f"last_error: {status.last_error}")
    return 0


def cmd_daemon_stop(args: argparse.Namespace) -> int:
    return DaemonService(_ctx(args)).stop()


def cmd_doctor(args: argparse.Namespace) -> int:
    report = ReportingService(_ctx(args)).doctor()
    for finding in report.findings:
        print(f"{finding.level.upper():4}  {finding.code}  {finding.message}")
    return int(report.exit_code)



def _print_decision(result: dict) -> None:
    print(f"decision: {result['decision_human_id']}")
    print(f"status: {result['status']}")
    print(f"action: {result['action']}")
    print(f"project: {result['project_human_id']}")
    print(f"requested_by: {result['requested_by']}")
    print(f"reason: {result['reason']}")
    print(f"impact: {result['impact']}")
    if result.get("decided_by"):
        print(f"decided_by: {result['decided_by']}")
    if result.get("execution_result"):
        print(f"execution: {result['execution_result']}")
    print(f"notice: {result['notice']}")


def cmd_decisions_list(args: argparse.Namespace) -> int:
    payload = ApprovalService(_ctx(args)).list_decisions(args.project, status=args.status)
    print(f"project: {payload['project_human_id']}")
    print(f"notice: {payload['notice']}")
    if not payload["decisions"]:
        print("(no decisions)")
        return 0
    for item in payload["decisions"]:
        print(
            f"  {item['decision_human_id']}  {item['status']}  {item['action']}  "
            f"target={item.get('target_human_id') or '-'}"
        )
    return 0


def cmd_decisions_open(args: argparse.Namespace) -> int:
    result = ApprovalService(_ctx(args)).open_decision(
        args.project,
        action=args.action,
        reason=args.reason,
        impact=args.impact,
        requested_by=args.actor,
        target_kind=args.target_kind,
        target_human_id=args.target,
    )
    _print_decision(result)
    return 0


def cmd_decisions_approve(args: argparse.Namespace) -> int:
    result = ApprovalService(_ctx(args)).approve_decision(
        args.project,
        args.decision,
        confirmed=args.confirm,
        actor=args.actor,
        reason=args.reason,
    )
    _print_decision(result)
    return 0


def cmd_decisions_reject(args: argparse.Namespace) -> int:
    result = ApprovalService(_ctx(args)).reject_decision(
        args.project,
        args.decision,
        confirmed=args.confirm,
        actor=args.actor,
        reason=args.reason,
    )
    _print_decision(result)
    return 0


def cmd_slack_setup(args: argparse.Namespace) -> int:
    print("ProjectOS Slack uses Socket Mode. No public URL or tunnel is required.")
    print("")
    print("1. Create the Slack app from integrations/slack/projectos-slack-manifest.yaml")
    print("2. Generate an App-Level Token (xapp-) with connections:write")
    print("3. Install the app and copy the Bot Token (xoxb-)")
    print("4. Open Settings → Integrations → Slack in the dashboard and save both tokens once")
    print("5. Add your #projectos channel as a global interface channel on the same page")
    print("6. In Slack run: /projectos use PRJ-### then /projectos status")
    return 0


def cmd_slack_doctor(args: argparse.Namespace) -> int:
    from projectos.slack_runtime import bootstrap_slack_credentials, current_slack_connection_status
    from projectos.slack_settings import read_slack_settings
    from projectos.slack_tokens import contains_secret

    creds = bootstrap_slack_credentials()
    settings = read_slack_settings(db_path=getattr(args, "db", None))
    runtime = current_slack_connection_status()
    blob = str(settings)
    if contains_secret(blob):
        print("error: Slack status leaked a secret", file=sys.stderr)
        return 1
    configured = "yes" if creds["configured"] else "no"
    print(f"configured: {configured}")
    print(f"source: {creds.get('storage') or 'none'}")
    print(f"app_token: {'configured' if creds['app_token_present'] else 'missing'}")
    print(f"bot_token: {'configured' if creds['bot_token_present'] else 'missing'}")
    if creds["configured"] and not creds["tokens_ready"]:
        print("token_validation: invalid_prefix")
    print("socket_mode: enabled")
    connection = str(runtime.get("connection_status") or settings["connection_status"])
    if creds["configured"] and connection == "not_configured":
        connection = "disconnected"
    print(f"connection: {connection}")
    print(f"workspace: {runtime.get('workspace_name') or settings.get('workspace_name') or 'not reported'}")
    if settings.get("bound_channels"):
        bound = settings["bound_channels"][0]
        print(f"bound: {bound['project_human_id']} channel {bound['channel_id']}")
    else:
        print("bound: none")
    detail = settings.get("detail") or "-"
    if creds["configured"] and settings["connection_status"] == "not_configured":
        detail = "Tokens are configured but adapter has not connected yet. Restart ProjectOS."
    print(f"detail: {detail}")
    return 0


def cmd_delivery_show(args: argparse.Namespace) -> int:
    from projectos.delivery.service import DeliveryService

    result = DeliveryService(_ctx(args)).show_contract(args.project)
    print(f"project: {result['project_human_id']}")
    print(f"adapter: {result['detected_adapter']}")
    contract = result["contract"]
    print(f"repository: {contract['repository_owner']}/{contract['repository_name']}")
    print(f"platforms: {', '.join(contract['target_platforms'])}")
    return 0


def cmd_delivery_validate(args: argparse.Namespace) -> int:
    from projectos.delivery.service import DeliveryService

    result = DeliveryService(_ctx(args)).validate_contract(args.project)
    print(f"ok: {result['ok']}")
    print(f"adapter: {result['adapter']}")
    print(f"repository: {result['repository']}")
    return 0


def cmd_release_prepare(args: argparse.Namespace) -> int:
    from projectos.delivery.service import DeliveryService

    result = DeliveryService(_ctx(args)).prepare_release(
        args.project,
        release_human_id=args.release_human_id,
        version=args.version,
        candidate_git_sha=args.git_sha,
    )
    print(f"release_record_id: {result['release_record_id']}")
    print(f"version: {result['version']}")
    print(f"candidate_git_sha: {result['candidate_git_sha']}")
    return 0


def cmd_release_package(args: argparse.Namespace) -> int:
    from projectos.delivery.service import DeliveryService

    result = DeliveryService(_ctx(args)).package_release(
        args.release_record_id,
        executor=args.executor,
    )
    print(f"release_record_id: {result['release_record_id']}")
    print(f"build_id: {result.get('build_id')}")
    for artifact in result.get("artifacts") or []:
        print(f"artifact: {artifact['artifact_name']} sha256={artifact['sha256']}")
    return 0


def cmd_release_verify(args: argparse.Namespace) -> int:
    from projectos.delivery.service import DeliveryService

    result = DeliveryService(_ctx(args)).verify_release(args.release_record_id)
    print(f"verified: {result['release_record_id']}")
    print(f"manifest_sha256: {result.get('manifest_sha256')}")
    return 0


def cmd_release_publish(args: argparse.Namespace) -> int:
    from projectos.delivery.service import DeliveryService

    result = DeliveryService(_ctx(args)).publish_release(args.release_record_id)
    print(f"publication_status: {result.get('publication_status')}")
    print(f"github_release_url: {result.get('github_release_url')}")
    return 0


def cmd_release_artifacts(args: argparse.Namespace) -> int:
    from projectos.delivery.service import DeliveryService

    result = DeliveryService(_ctx(args)).get_release(args.release_record_id)
    for artifact in result.get("artifacts") or []:
        print(
            f"{artifact['artifact_id']}  {artifact['artifact_type']}  "
            f"{artifact['artifact_name']}  {artifact['sha256']}"
        )
    return 0


def cmd_release_manifest(args: argparse.Namespace) -> int:
    from projectos.delivery.service import DeliveryService

    result = DeliveryService(_ctx(args)).get_release(args.release_record_id)
    for gate in result.get("gates") or []:
        print(f"{gate['gate_name']}: {gate['status']}  {gate.get('detail') or ''}")
    return 0


def cmd_github_doctor(args: argparse.Namespace) -> int:
    from projectos.github.secret_setup import read_github_settings
    from projectos.github.tokens import contains_secret

    settings = read_github_settings()
    blob = str(settings)
    if contains_secret(blob):
        print("error: GitHub status leaked a secret", file=sys.stderr)
        return 1
    print(f"configured: {'yes' if settings['configured'] else 'no'}")
    print(f"source: {settings['token_source']}")
    print(f"storage: {settings['storage']}")
    return 0


def cmd_openai_doctor(args: argparse.Namespace) -> int:
    from projectos.openai_config import openai_enabled, openai_model
    from projectos.openai_settings import read_openai_settings
    from projectos.openai_state import connection_status
    from projectos.openai_tokens import api_key_source, contains_secret, token_report
    from projectos.slack_chatgpt_config import chatgpt_slack_user_id, chatgpt_slack_user_id_source

    settings = read_openai_settings()
    blob = str(settings)
    if contains_secret(blob):
        print("error: OpenAI status leaked a secret", file=sys.stderr)
        return 1
    report = token_report()
    configured = "yes" if report["api_key_configured"] else "no"
    enabled = "yes" if openai_enabled() else "no"
    source = api_key_source()
    trigger = chatgpt_slack_user_id()
    trigger_status = "configured" if trigger else "not configured"
    print(f"configured: {configured}")
    print(f"enabled: {enabled}")
    print(f"source: {source}")
    print(f"model: {settings['model'] or openai_model()}")
    print(f"trigger_user_id: {trigger_status}")
    if trigger:
        print(f"trigger_user_id_value: {trigger}")
    print(f"trigger_user_id_source: {chatgpt_slack_user_id_source()}")
    print(f"connection: {connection_status()}")
    if not report["api_key_configured"]:
        print("action: configure OpenAI API key in Settings or set PROJECTOS_OPENAI_API_KEY")
        return 1
    if getattr(args, "probe", False):
        from projectos.openai_client import probe_api

        try:
            result = probe_api()
        except Exception as exc:
            detail = str(exc)
            if contains_secret(detail):
                detail = "probe failed"
            print(f"probe: failed")
            print(f"reason: {detail}")
            return 1
        print(f"probe: success")
        print(f"response_id: {result.get('response_id')}")
    else:
        print("probe: skipped (pass --probe to make one billable API call)")
    return 0


def cmd_slack_list(args: argparse.Namespace) -> int:
    payload = SlackBindingService(_ctx(args)).list_bindings(args.project)
    print(f"project: {payload['project_human_id']}")
    print(f"repository_root: {payload['repository_root']}")
    print(f"notice: {payload['notice']}")
    if not payload["bindings"]:
        print("(no slack bindings)")
        return 0
    for item in payload["bindings"]:
        thread = item.get("thread_ts") or "-"
        print(
            f"  {item['binding_human_id']}  channel={item['channel_id']}  "
            f"thread={thread}  team={item.get('team_id') or '-'}"
        )
    return 0


def cmd_slack_bind(args: argparse.Namespace) -> int:
    result = SlackBindingService(_ctx(args)).bind(
        args.project,
        channel_id=args.channel,
        team_id=args.team,
        thread_ts=args.thread,
    )
    print(f"binding: {result['binding_human_id']}")
    print(f"project: {result['project_human_id']}")
    print(f"channel: {result['channel_id']}")
    print(f"repository_root: {result['repository_root']}")
    return 0


def cmd_slack_unbind(args: argparse.Namespace) -> int:
    result = SlackBindingService(_ctx(args)).unbind(
        args.project,
        channel_id=args.channel,
        team_id=args.team,
        thread_ts=args.thread,
    )
    print(f"unbound: {result['binding_human_id']}")
    return 0


def cmd_slack_inbound(args: argparse.Namespace) -> int:
    result = SlackBindingService(_ctx(args)).inbound(
        channel_id=args.channel,
        team_id=args.team,
        thread_ts=args.thread,
        message_ts=args.message,
        project_human_id=args.project,
    )
    print(f"project: {result['project_human_id']}")
    print(f"resolved_via: {result['resolved_via']}")
    print(f"repository_root: {result['repository_root']}")
    print(f"notice: {result['notice']}")
    return 0


def cmd_slack_command(args: argparse.Namespace) -> int:
    result = SlackBindingService(_ctx(args)).command(
        command=args.command_name,
        channel_id=args.channel,
        team_id=args.team,
        thread_ts=args.thread,
        message_ts=args.message,
        project_human_id=args.project,
        title=getattr(args, "title", None),
        description=getattr(args, "description", None),
        source=getattr(args, "source", None),
    )
    print(result["text"])
    if result.get("item_human_id"):
        print(f"item: {result['item_kind']} {result['item_human_id']}")
    return 0


def cmd_slack_notify(args: argparse.Namespace) -> int:
    result = SlackBindingService(_ctx(args)).notify(args.project)
    print(f"project: {result['project_human_id']}")
    print(f"notice: {result['notice']}")
    if not result["posted"]:
        print("(no new notifications)")
        return 0
    for item in result["posted"]:
        print(f"  {item['kind']}  {item['entity_human_id']}  {item['dashboard_path']}")
    return 0


def cmd_learning_retire(args: argparse.Namespace) -> int:
    result = MemoryAdminService(_ctx(args)).retire(
        args.project,
        args.memory,
        confirmed=args.confirm,
        reason=args.reason,
        actor=args.actor,
    )
    memory = result["memory"]
    print(f"action: {result['action']}")
    print(f"memory: {memory['memory_human_id']}")
    print(f"status: {memory['status']}")
    print(f"actor: {result['actor']}")
    print(f"reason: {result['reason']}")
    return 0


def cmd_learning_supersede(args: argparse.Namespace) -> int:
    result = MemoryAdminService(_ctx(args)).supersede(
        args.project,
        args.memory,
        successor_title=args.successor_title,
        confirmed=args.confirm,
        reason=args.reason,
        actor=args.actor,
        evidence_ref=args.evidence_ref,
    )
    memory = result["memory"]
    successor = result["successor"] or {}
    print(f"action: {result['action']}")
    print(f"memory: {memory['memory_human_id']}")
    print(f"status: {memory['status']}")
    print(f"successor: {successor.get('memory_human_id')}")
    print(f"actor: {result['actor']}")
    print(f"reason: {result['reason']}")
    return 0


def cmd_fat_reconcile(args: argparse.Namespace) -> int:
    """Governed FAT reconciliation — does not dispatch workers."""
    result = RecoverService(_ctx(args)).reconcile_fat(
        args.project,
        args.iteration,
        skip_work_items=args.skip_work_items,
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
    p_reg_add = reg_sub.add_parser(
        "register",
        help="Governed onboarding of a delivery repository (no manual JSON edit)",
    )
    p_reg_add.add_argument("repository_path", type=Path)
    p_reg_add.set_defaults(func=cmd_registry_register)
    p_reg_upd = reg_sub.add_parser(
        "update",
        help="Re-validate and refresh a registered project",
    )
    p_reg_upd.add_argument("project_human_id")
    p_reg_upd.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Optional repository path (default: existing registered root)",
    )
    p_reg_upd.set_defaults(func=cmd_registry_update)
    p_reg_dis = reg_sub.add_parser(
        "disable",
        help="Disable a registered project without deleting it",
    )
    p_reg_dis.add_argument("project_human_id")
    p_reg_dis.set_defaults(func=cmd_registry_disable)

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
        "--salvage-candidate",
        action="store_true",
        help=(
            "Governed salvage of a committed worktree candidate after FAILED "
            "DELIVERY (preserves failure history; creates assurance jobs; "
            "does not dispatch)"
        ),
    )
    p_recover.add_argument(
        "--reconcile-release",
        action="store_true",
        dest="reconcile_release",
        help=(
            "Governed reconciliation of a stale RELEASE attempt (preserves "
            "historical attempt; creates a successor bound to the integrated "
            "candidate; does not dispatch)"
        ),
    )
    p_recover.add_argument(
        "--retry-release",
        action="store_true",
        dest="reconcile_release",
        help="Alias for --reconcile-release",
    )
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
        help=(
            "Job human id (for --salvage-candidate / --reconcile-release / "
            "--revalidate-blocked / --reclaim-running)"
        ),
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

    p_start = sub.add_parser("start", help="Start API, dashboard, daemon, and Slack adapter locally")
    p_start.add_argument(
        "--wait",
        action="store_true",
        help="Stay attached and stop all children on exit",
    )
    _add_db_arg(p_start)
    p_start.set_defaults(func=cmd_start)
    p_dashboard = sub.add_parser("dashboard", help="Dashboard build commands")
    dashboard_sub = p_dashboard.add_subparsers(dest="dashboard_command", required=True)
    p_dashboard_build = dashboard_sub.add_parser(
        "build",
        help="Build the dashboard SPA into web/dist when sources are newer than the bundle",
    )
    p_dashboard_build.add_argument(
        "--force",
        action="store_true",
        help="Rebuild even when web/dist is already up to date",
    )
    p_dashboard_build.set_defaults(func=cmd_dashboard_build)
    p_stop = sub.add_parser("stop", help="Stop local operator processes")
    _add_db_arg(p_stop)
    p_stop.set_defaults(func=cmd_stop)
    p_status = sub.add_parser("status", help="Show local operator component readiness")
    _add_db_arg(p_status)
    p_status.set_defaults(func=cmd_status)

    p_api = sub.add_parser("api", help="Run the local versioned HTTP control plane")
    p_api.add_argument("--host", default="127.0.0.1", help="Bind address (default loopback)")
    p_api.add_argument("--port", type=int, default=8787)
    _add_db_arg(p_api)
    p_api.set_defaults(func=cmd_api)

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

    p_learn = sub.add_parser("learning", help="Governed AGENT_MEMORY administration")
    learn_sub = p_learn.add_subparsers(dest="learning_command", required=True)
    p_learn_ret = learn_sub.add_parser("retire", help="Retire an ACTIVE memory")
    p_learn_ret.add_argument("--project", required=True)
    p_learn_ret.add_argument("--memory", required=True)
    p_learn_ret.add_argument("--actor", required=True)
    p_learn_ret.add_argument("--reason", required=True)
    p_learn_ret.add_argument("--confirm", action="store_true")
    _add_db_arg(p_learn_ret)
    p_learn_ret.set_defaults(func=cmd_learning_retire)
    p_learn_sup = learn_sub.add_parser("supersede", help="Supersede an ACTIVE memory")
    p_learn_sup.add_argument("--project", required=True)
    p_learn_sup.add_argument("--memory", required=True)
    p_learn_sup.add_argument("--successor-title", required=True)
    p_learn_sup.add_argument("--actor", required=True)
    p_learn_sup.add_argument("--reason", required=True)
    p_learn_sup.add_argument("--evidence-ref", default=None)
    p_learn_sup.add_argument("--confirm", action="store_true")
    _add_db_arg(p_learn_sup)
    p_learn_sup.set_defaults(func=cmd_learning_supersede)

    p_dec = sub.add_parser("decisions", help="Sponsor decision requests (explicit approve/reject)")
    dec_sub = p_dec.add_subparsers(dest="decisions_command", required=True)
    p_dec_list = dec_sub.add_parser("list", help="List decision requests")
    p_dec_list.add_argument("--project", required=True)
    p_dec_list.add_argument("--status", default=None)
    _add_db_arg(p_dec_list)
    p_dec_list.set_defaults(func=cmd_decisions_list)
    p_dec_open = dec_sub.add_parser("open", help="Open a governed decision request")
    p_dec_open.add_argument("--project", required=True)
    p_dec_open.add_argument("--action", required=True)
    p_dec_open.add_argument("--reason", required=True)
    p_dec_open.add_argument("--impact", required=True)
    p_dec_open.add_argument("--actor", required=True)
    p_dec_open.add_argument("--target-kind", default="none")
    p_dec_open.add_argument("--target", default=None)
    _add_db_arg(p_dec_open)
    p_dec_open.set_defaults(func=cmd_decisions_open)
    p_dec_ok = dec_sub.add_parser("approve", help="Sponsor approve (confirmation required)")
    p_dec_ok.add_argument("--project", required=True)
    p_dec_ok.add_argument("--decision", required=True)
    p_dec_ok.add_argument("--actor", required=True)
    p_dec_ok.add_argument("--reason", required=True)
    p_dec_ok.add_argument("--confirm", action="store_true")
    _add_db_arg(p_dec_ok)
    p_dec_ok.set_defaults(func=cmd_decisions_approve)
    p_dec_no = dec_sub.add_parser("reject", help="Sponsor reject (confirmation required)")
    p_dec_no.add_argument("--project", required=True)
    p_dec_no.add_argument("--decision", required=True)
    p_dec_no.add_argument("--actor", required=True)
    p_dec_no.add_argument("--reason", required=True)
    p_dec_no.add_argument("--confirm", action="store_true")
    _add_db_arg(p_dec_no)
    p_dec_no.set_defaults(func=cmd_decisions_reject)

    p_slack = sub.add_parser("slack", help="Bind Slack channels/threads to registered projects")
    slack_sub = p_slack.add_subparsers(dest="slack_command", required=True)
    p_slack_setup = slack_sub.add_parser("setup", help="Print Socket Mode setup steps (no secrets)")
    p_slack_setup.set_defaults(func=cmd_slack_setup)
    p_slack_doctor = slack_sub.add_parser("doctor", help="Show Slack Socket Mode status without secrets")
    _add_db_arg(p_slack_doctor)
    p_slack_doctor.set_defaults(func=cmd_slack_doctor)

    p_openai = sub.add_parser("openai", help="OpenAI ChatGPT advisor integration")
    openai_sub = p_openai.add_subparsers(dest="openai_command", required=True)
    p_openai_doctor = openai_sub.add_parser("doctor", help="Show OpenAI integration status without secrets")
    p_openai_doctor.add_argument(
        "--probe",
        action="store_true",
        help="Make one minimal billable Responses API call",
    )
    p_openai_doctor.set_defaults(func=cmd_openai_doctor)

    p_slack_list = slack_sub.add_parser("list", help="List Slack bindings for a project")
    p_slack_list.add_argument("--project", required=True)
    _add_db_arg(p_slack_list)
    p_slack_list.set_defaults(func=cmd_slack_list)
    p_slack_bind = slack_sub.add_parser("bind", help="Bind a Slack channel or thread")
    p_slack_bind.add_argument("--project", required=True)
    p_slack_bind.add_argument("--channel", required=True)
    p_slack_bind.add_argument("--team", default=None)
    p_slack_bind.add_argument("--thread", default=None)
    _add_db_arg(p_slack_bind)
    p_slack_bind.set_defaults(func=cmd_slack_bind)
    p_slack_un = slack_sub.add_parser("unbind", help="Remove a Slack binding")
    p_slack_un.add_argument("--project", required=True)
    p_slack_un.add_argument("--channel", required=True)
    p_slack_un.add_argument("--team", default=None)
    p_slack_un.add_argument("--thread", default=None)
    _add_db_arg(p_slack_un)
    p_slack_un.set_defaults(func=cmd_slack_unbind)
    p_slack_in = slack_sub.add_parser("inbound", help="Resolve a Slack request into a project")
    p_slack_in.add_argument("--channel", required=True)
    p_slack_in.add_argument("--team", default=None)
    p_slack_in.add_argument("--thread", default=None)
    p_slack_in.add_argument("--message", default=None)
    p_slack_in.add_argument("--project", default=None, help="Explicit registered project id")
    _add_db_arg(p_slack_in)
    p_slack_in.set_defaults(func=cmd_slack_inbound)
    for name, help_text in (
        ("status", "Project status summary"),
        ("iteration", "Current iteration status"),
        ("blockers", "Failed/blocked jobs and release blockers"),
        ("qa", "QA summary"),
        ("release", "Release status"),
        ("reports", "Report links"),
        ("learning", "Learning summary"),
    ):
        parser_cmd = slack_sub.add_parser(name, help=help_text)
        parser_cmd.add_argument("--channel", required=True)
        parser_cmd.add_argument("--team", default=None)
        parser_cmd.add_argument("--thread", default=None)
        parser_cmd.add_argument("--message", default=None)
        parser_cmd.add_argument("--project", default=None)
        _add_db_arg(parser_cmd)
        parser_cmd.set_defaults(func=cmd_slack_command, command_name=name)
    for name, help_text in (
        ("feedback", "Record customer/QA feedback as a projectctl story"),
        ("defect", "Record a defect without Slack-set severity/priority"),
    ):
        parser_cmd = slack_sub.add_parser(name, help=help_text)
        parser_cmd.add_argument("--channel", required=True)
        parser_cmd.add_argument("--title", required=True)
        parser_cmd.add_argument("--description", default="")
        parser_cmd.add_argument("--source", default="slack")
        parser_cmd.add_argument("--team", default=None)
        parser_cmd.add_argument("--thread", default=None)
        parser_cmd.add_argument("--message", default=None)
        parser_cmd.add_argument("--project", default=None)
        _add_db_arg(parser_cmd)
        parser_cmd.set_defaults(func=cmd_slack_command, command_name=name)
    p_slack_notify = slack_sub.add_parser(
        "notify",
        help="Post idempotent iteration/release notices to bound Slack channels",
    )
    p_slack_notify.add_argument("--project", required=True)
    _add_db_arg(p_slack_notify)
    p_slack_notify.set_defaults(func=cmd_slack_notify)

    p_delivery = sub.add_parser("delivery", help="Universal software delivery pipeline")
    delivery_sub = p_delivery.add_subparsers(dest="delivery_command", required=True)
    p_delivery_show = delivery_sub.add_parser("show", help="Show delivery contract for a project")
    p_delivery_show.add_argument("--project", required=True)
    _add_db_arg(p_delivery_show)
    p_delivery_show.set_defaults(func=cmd_delivery_show)
    p_delivery_validate = delivery_sub.add_parser("validate", help="Validate delivery contract and adapter")
    p_delivery_validate.add_argument("--project", required=True)
    _add_db_arg(p_delivery_validate)
    p_delivery_validate.set_defaults(func=cmd_delivery_validate)

    p_release = sub.add_parser("release", help="Delivery release operations")
    release_sub = p_release.add_subparsers(dest="release_command", required=True)
    p_release_prepare = release_sub.add_parser("prepare", help="Prepare a delivery release record")
    p_release_prepare.add_argument("--project", required=True)
    p_release_prepare.add_argument("--release", required=True, dest="release_human_id")
    p_release_prepare.add_argument("--version", required=True)
    p_release_prepare.add_argument("--git-sha", default=None)
    _add_db_arg(p_release_prepare)
    p_release_prepare.set_defaults(func=cmd_release_prepare)
    p_release_package = release_sub.add_parser("package", help="Build and package a delivery release")
    p_release_package.add_argument("--record", required=True, dest="release_record_id")
    p_release_package.add_argument("--executor", default="LOCAL", choices=["LOCAL", "CI"])
    _add_db_arg(p_release_package)
    p_release_package.set_defaults(func=cmd_release_package)
    p_release_verify = release_sub.add_parser("verify", help="Verify artifacts and manifest")
    p_release_verify.add_argument("--record", required=True, dest="release_record_id")
    _add_db_arg(p_release_verify)
    p_release_verify.set_defaults(func=cmd_release_verify)
    p_release_publish = release_sub.add_parser("publish", help="Publish release to GitHub")
    p_release_publish.add_argument("--record", required=True, dest="release_record_id")
    _add_db_arg(p_release_publish)
    p_release_publish.set_defaults(func=cmd_release_publish)
    p_release_artifacts = release_sub.add_parser("artifacts", help="List delivery artifacts")
    p_release_artifacts.add_argument("--record", required=True, dest="release_record_id")
    _add_db_arg(p_release_artifacts)
    p_release_artifacts.set_defaults(func=cmd_release_artifacts)
    p_release_manifest = release_sub.add_parser("manifest", help="Show release gate status")
    p_release_manifest.add_argument("--record", required=True, dest="release_record_id")
    _add_db_arg(p_release_manifest)
    p_release_manifest.set_defaults(func=cmd_release_manifest)

    p_github = sub.add_parser("github", help="GitHub integration")
    github_sub = p_github.add_subparsers(dest="github_command", required=True)
    p_github_doctor = github_sub.add_parser("doctor", help="Show GitHub integration status")
    p_github_doctor.set_defaults(func=cmd_github_doctor)

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
