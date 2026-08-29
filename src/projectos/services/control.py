"""Safe GUI control operations: dispatch, pause, recovery, daemon status."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from projectos.daemon import get_daemon_status
from projectos.db import connection
from projectos.dispatch import DispatchResult, run_dispatch
from projectos.errors import ConflictError, OrchestrationError
from projectos.migrate import initialize_database
from projectos.recover import RecoveryReport, preview_recovery, run_recovery
from projectos.schedule import is_due_now, list_schedules
from projectos.services.context import ServiceContext
from projectos.services.facades import ProjectQueryService, WorkerService
from projectos.store import (
    acquire_operation_lock,
    get_idempotency_record,
    get_job_by_human_id,
    get_orchestration_control,
    is_project_paused,
    list_eligible_ready_jobs,
    put_idempotency_record,
    release_operation_lock,
    set_project_paused,
    utc_now,
)
from projectos.worker import WorkerResult


def _lock_name(project_human_id: str) -> str:
    return f"project:{project_human_id}:control"


def _fingerprint(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _worker_dict(result: WorkerResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "job_human_id": result.job_human_id,
        "message": result.message,
        "exit_code": result.exit_code,
    }


def _dispatch_dict(result: DispatchResult) -> dict[str, Any]:
    return {
        "mode": result.mode,
        "message": result.message,
        "cancelled": result.cancelled,
        "completed": [_worker_dict(item) for item in result.completed],
    }


def _recovery_dict(report: RecoveryReport) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "expired_lease_job_ids": list(report.expired_lease_job_ids),
        "promoted_ready": list(report.promoted_ready),
        "blocked": list(report.blocked),
        "identity_checks": [
            {
                "project_human_id": check.project_human_id,
                "ok": check.ok,
                "error": check.error,
            }
            for check in report.identity_checks
        ],
        "worktree_actions": [
            {
                "job_human_id": action.job_human_id,
                "action": action.action,
                "message": action.message,
            }
            for action in report.worktree_actions
        ],
        "messages": list(report.messages),
    }


@dataclass(frozen=True)
class IdempotentResult:
    status_code: int
    payload: dict[str, Any]
    replayed: bool = False


class ControlService:
    """Project-scoped control plane. Never accepts shell commands or workspace paths."""

    def __init__(
        self,
        ctx: ServiceContext,
        *,
        cursor_runner: Callable[..., Any] | None = None,
        projectctl_runner=None,
        skip_identity_validation: bool = False,
    ) -> None:
        self.ctx = ctx
        self.cursor_runner = cursor_runner
        self.projectctl_runner = projectctl_runner
        self.skip_identity_validation = skip_identity_validation
        self._projects = ProjectQueryService(ctx)
        self._workers = WorkerService(ctx)

    def _require_project(self, project_human_id: str) -> None:
        self._projects._require_project(project_human_id)

    def _require_unpaused(self, project_human_id: str) -> None:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            if is_project_paused(conn, project_human_id):
                raise ConflictError(
                    f"orchestration for {project_human_id} is paused"
                )

    def replay_or_begin(
        self,
        *,
        project_human_id: str,
        operation: str,
        idempotency_key: str | None,
        fingerprint: dict[str, Any],
    ) -> IdempotentResult | None:
        if not idempotency_key:
            return None
        initialize_database(self.ctx.db_path)
        scope = f"{project_human_id}:{operation}"
        fp = _fingerprint(fingerprint)
        with connection(self.ctx.db_path) as conn:
            existing = get_idempotency_record(conn, scope, idempotency_key)
        if existing is None:
            return None
        if existing["fingerprint"] != fp:
            raise ConflictError(
                "idempotency key was already used with a different request"
            )
        return IdempotentResult(
            status_code=int(existing["status_code"]),
            payload=json.loads(existing["response_json"]),
            replayed=True,
        )

    def remember(
        self,
        *,
        project_human_id: str,
        operation: str,
        idempotency_key: str | None,
        fingerprint: dict[str, Any],
        status_code: int,
        payload: dict[str, Any],
    ) -> None:
        if not idempotency_key:
            return
        initialize_database(self.ctx.db_path)
        scope = f"{project_human_id}:{operation}"
        with connection(self.ctx.db_path) as conn:
            put_idempotency_record(
                conn,
                scope=scope,
                idempotency_key=idempotency_key,
                fingerprint=_fingerprint(fingerprint),
                status_code=status_code,
                response_json=json.dumps(payload, sort_keys=True),
            )

    def _with_lock(self, project_human_id: str, fn):
        initialize_database(self.ctx.db_path)
        lock = _lock_name(project_human_id)
        owner = uuid.uuid4().hex
        with connection(self.ctx.db_path) as conn:
            acquire_operation_lock(conn, lock, owner=owner)
        try:
            return fn()
        finally:
            with connection(self.ctx.db_path) as conn:
                release_operation_lock(conn, lock)

    def orchestration_status(self, project_human_id: str) -> dict[str, Any]:
        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            control = get_orchestration_control(conn, project_human_id)
            eligible = list_eligible_ready_jobs(
                conn, project_human_id=project_human_id
            )
        return {
            "project_human_id": project_human_id,
            "paused": control["paused"],
            "paused_reason": control["paused_reason"],
            "updated_at": control["updated_at"],
            "eligible_job_ids": [job.human_id for job in eligible],
        }

    def pause(self, project_human_id: str, *, reason: str | None = None) -> dict[str, Any]:
        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            set_project_paused(conn, project_human_id, paused=True, reason=reason)
        return self.orchestration_status(project_human_id)

    def resume(self, project_human_id: str) -> dict[str, Any]:
        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            set_project_paused(conn, project_human_id, paused=False)
        return self.orchestration_status(project_human_id)

    def dispatch_run_once(
        self,
        project_human_id: str,
        *,
        job_human_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_project(project_human_id)
        self._require_unpaused(project_human_id)
        if job_human_id and ("/" in job_human_id or "\\" in job_human_id):
            raise OrchestrationError("job_human_id must not contain a path")

        def _run() -> dict[str, Any]:
            initialize_database(self.ctx.db_path)
            chosen = job_human_id
            with connection(self.ctx.db_path) as conn:
                if chosen:
                    job = get_job_by_human_id(conn, chosen)
                    if job is None or job.project_human_id != project_human_id:
                        raise OrchestrationError(f"job {job_human_id!r} not found")
                else:
                    eligible = list_eligible_ready_jobs(
                        conn, project_human_id=project_human_id, limit=1
                    )
                    if not eligible:
                        return {
                            "mode": "once",
                            "message": "no eligible READY jobs",
                            "cancelled": False,
                            "completed": [],
                        }
                    chosen = eligible[0].human_id
            result = self._workers.run_once(
                job_human_id=chosen,
                cursor_runner=self.cursor_runner,
                projectctl_runner=self.projectctl_runner,
                skip_identity_validation=self.skip_identity_validation,
            )
            return {
                "mode": "once",
                "message": result.message,
                "cancelled": False,
                "completed": [_worker_dict(result)],
            }

        return self._with_lock(project_human_id, _run)

    def dispatch_run_until_idle(
        self,
        project_human_id: str,
        *,
        max_parallel: int = 3,
    ) -> dict[str, Any]:
        self._require_project(project_human_id)
        self._require_unpaused(project_human_id)
        parallel = max(1, min(8, int(max_parallel)))

        def _run() -> dict[str, Any]:
            result = run_dispatch(
                until_idle=True,
                max_parallel=parallel,
                db_path=self.ctx.db_path,
                registry_path=self.ctx.registry_path,
                cursor_runner=self.cursor_runner,
                projectctl_runner=self.projectctl_runner,
                skip_identity_validation=self.skip_identity_validation,
                project_human_id=project_human_id,
            )
            return _dispatch_dict(result)

        return self._with_lock(project_human_id, _run)

    def recovery_preview(self, project_human_id: str) -> dict[str, Any]:
        self._require_project(project_human_id)
        report = preview_recovery(
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            projectctl_runner=self.projectctl_runner,
            project_human_id=project_human_id,
        )
        payload = _recovery_dict(report)
        payload["dry_run"] = True
        payload["project_human_id"] = project_human_id
        return payload

    def recovery_execute(self, project_human_id: str) -> dict[str, Any]:
        self._require_project(project_human_id)

        def _run() -> dict[str, Any]:
            report = run_recovery(
                db_path=self.ctx.db_path,
                registry_path=self.ctx.registry_path,
                projectctl_runner=self.projectctl_runner,
                project_human_id=project_human_id,
            )
            payload = _recovery_dict(report)
            payload["dry_run"] = False
            payload["project_human_id"] = project_human_id
            return payload

        return self._with_lock(project_human_id, _run)

    def daemon_status(self) -> dict[str, Any]:
        status = get_daemon_status(self.ctx.db_path)
        return {
            "status": status.status,
            "pid": status.pid,
            "heartbeat_at": status.heartbeat_at,
            "started_at": status.started_at,
            "last_error": status.last_error,
            "lock_path": status.lock_path,
        }

    def scheduler_status(self) -> dict[str, Any]:
        initialize_database(self.ctx.db_path)
        daemon = self.daemon_status()
        now = utc_now()
        with connection(self.ctx.db_path) as conn:
            entries = list_schedules(conn)
            paused = {
                str(row["project_human_id"]): bool(row["paused"])
                for row in conn.execute(
                    "SELECT project_human_id, paused FROM project_orchestration_control"
                ).fetchall()
            }
        due = []
        for entry in entries:
            reached, key = is_due_now(entry, now)
            due.append(
                {
                    "project_human_id": entry.project_human_id,
                    "enabled": entry.enabled,
                    "paused": bool(paused.get(entry.project_human_id)),
                    "window_key": key,
                    "due": bool(
                        reached
                        and entry.enabled
                        and not paused.get(entry.project_human_id)
                    ),
                    "cadence": entry.cadence,
                    "local_time": entry.local_time,
                }
            )
        return {"daemon": daemon, "schedules": due}
