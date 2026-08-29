"""Application-service façades over existing ProjectOS callables.

These are the Python API for operators and (later) HTTP adapters. They do not
invent orchestration policy: they bind a ServiceContext and delegate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from projectos.budget import BudgetReport, build_budget_report
from projectos.daemon import DaemonStatus, get_daemon_status, run_daemon, stop_daemon
from projectos.db import connection
from projectos.dispatch import DispatchResult, run_dispatch
from projectos.doctor import DoctorReport, run_doctor
from projectos.errors import OrchestrationError, RegistryError
from projectos.invalidate import FatReconcileResult, reconcile_prj003_iter002_fat
from projectos.onboarding import (
    OnboardingResult,
    disable_project,
    register_project,
    update_project,
)
from projectos.iteration import IterationResult, run_iteration
from projectos.migrate import initialize_database
from projectos.plan import PlanResult, load_latest_accepted_plan, run_plan, validate_plan_document
from projectos.projectctl_bridge import ProjectctlStatusResult, run_projectctl_status
from projectos.recover import RecoveryReport, RevalidateBlockedResult, run_recovery, revalidate_blocked_job
from projectos.registry import (
    ProjectRegistry,
    RegistryEntry,
    load_registry,
    load_registry_or_empty,
)
from projectos.release_readiness import ReleaseEvaluation, assemble_qa_package, evaluate_release_job
from projectos.release_retry import ReleaseReconcileResult, reconcile_stale_release
from projectos.salvage import SalvageResult, salvage_delivery_candidate
from projectos.schedule import ScheduleReport, evaluate_due, list_schedules, upsert_schedule
from projectos.services.context import ServiceContext
from projectos.store import (
    OrchestrationJob,
    RunEventRecord,
    get_job_by_human_id,
    list_dependency_graph_for_project,
    list_agent_runs_for_project,
    list_assurance_for_project,
    list_eligible_ready_jobs,
    list_integrations_for_project,
    list_invalidations_for_project,
    list_jobs_for_project,
    list_release_artifacts,
    list_report_snapshots,
    get_report_snapshot,
    upsert_report_snapshot,
    list_run_events_for_project,
    promote_retry_wait_to_ready,
    reclaim_interrupted_running_job,
)
from projectos.validation import ValidationReport, validate_registry
from projectos.worker import WorkerResult, run_once


def _require_job_id(job_human_id: str | None, operator: str) -> str:
    if not job_human_id:
        raise OrchestrationError(f"{operator} requires --job <job_human_id>")
    return job_human_id


class RegistryService:
    """Registry listing and onboarding/identity validation."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def load(self) -> ProjectRegistry:
        return load_registry(self.ctx.registry_path)

    def list_projects(self) -> tuple[RegistryEntry, ...]:
        return load_registry_or_empty(self.ctx.registry_path).projects

    def show(self, project_human_id: str) -> RegistryEntry:
        entry = self.load().get(project_human_id)
        if entry is None:
            raise RegistryError(
                f"project {project_human_id!r} is not in the registry"
            )
        return entry

    def validate(
        self,
        project_human_id: str | None = None,
        *,
        projectctl_runner=None,
    ) -> ValidationReport:
        return validate_registry(
            path=self.ctx.registry_path,
            project_human_id=project_human_id,
            projectctl_runner=projectctl_runner,
        )

    def register(
        self,
        repository_path: Path | str,
        *,
        projectctl_runner=None,
    ) -> OnboardingResult:
        return register_project(
            repository_path,
            registry_path=self.ctx.registry_path,
            projectctl_runner=projectctl_runner,
        )

    def update(
        self,
        project_human_id: str,
        *,
        repository_path: Path | str | None = None,
        projectctl_runner=None,
    ) -> OnboardingResult:
        return update_project(
            project_human_id,
            repository_path=repository_path,
            registry_path=self.ctx.registry_path,
            projectctl_runner=projectctl_runner,
        )

    def disable(self, project_human_id: str) -> OnboardingResult:
        return disable_project(
            project_human_id,
            registry_path=self.ctx.registry_path,
        )


class PlanService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def run(
        self,
        project_human_id: str,
        *,
        dry_run: bool = False,
        iteration_human_id: str | None = None,
        cursor_runner: Callable[..., Any] | None = None,
        projectctl_runner=None,
        plan_override: dict[str, Any] | None = None,
        work_request: dict[str, Any] | None = None,
    ) -> PlanResult:
        return run_plan(
            project_human_id=project_human_id,
            dry_run=dry_run,
            iteration_human_id=iteration_human_id,
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            cursor_runner=cursor_runner,
            projectctl_runner=projectctl_runner,
            plan_override=plan_override,
            work_request=work_request,
        )


class WorkerService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def run_once(
        self,
        *,
        queue: str | None = None,
        role: str | None = None,
        job_human_id: str | None = None,
        lease_seconds: int = 900,
        timeout_seconds: float = 1800.0,
        cursor_runner: Callable[..., Any] | None = None,
        projectctl_runner=None,
        skip_identity_validation: bool = False,
        cancel_event=None,
        release_evaluator=None,
    ) -> WorkerResult:
        return run_once(
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            queue=queue,
            role=role,
            job_human_id=job_human_id,
            lease_seconds=lease_seconds,
            timeout_seconds=timeout_seconds,
            cursor_runner=cursor_runner,
            projectctl_runner=projectctl_runner,
            skip_identity_validation=skip_identity_validation,
            cancel_event=cancel_event,
            release_evaluator=release_evaluator,
        )


class DispatchService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def run(
        self,
        *,
        once: bool = False,
        until_idle: bool = False,
        max_parallel: int = 3,
        lease_seconds: int = 900,
        timeout_seconds: float = 1800.0,
        cursor_runner: Callable[..., Any] | None = None,
        projectctl_runner=None,
        skip_identity_validation: bool = False,
        cancel_event=None,
        project_human_id: str | None = None,
    ) -> DispatchResult:
        return run_dispatch(
            once=once,
            until_idle=until_idle,
            max_parallel=max_parallel,
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            lease_seconds=lease_seconds,
            timeout_seconds=timeout_seconds,
            cursor_runner=cursor_runner,
            projectctl_runner=projectctl_runner,
            skip_identity_validation=skip_identity_validation,
            cancel_event=cancel_event,
            project_human_id=project_human_id,
        )


@dataclass(frozen=True)
class ReclaimRunningResult:
    job_human_id: str
    status_before: str
    status_after: str
    attempt: int
    last_error: str | None
    base_git_sha: str | None
    worktree_path: str | None


class RecoverService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def run(
        self,
        *,
        promote_retry_wait: bool = True,
        projectctl_runner=None,
        project_human_id: str | None = None,
    ) -> RecoveryReport:
        return run_recovery(
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            projectctl_runner=projectctl_runner,
            promote_retry_wait=promote_retry_wait,
            project_human_id=project_human_id,
        )

    def preview(self, *, projectctl_runner=None, project_human_id: str | None = None):
        from projectos.recover import preview_recovery

        return preview_recovery(
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            projectctl_runner=projectctl_runner,
            project_human_id=project_human_id,
        )

    def salvage(self, job_human_id: str | None) -> SalvageResult:
        hid = _require_job_id(job_human_id, "--salvage-candidate")
        return salvage_delivery_candidate(
            job_human_id=hid,
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
        )

    def reconcile_release(self, job_human_id: str | None) -> ReleaseReconcileResult:
        hid = _require_job_id(job_human_id, "--reconcile-release")
        return reconcile_stale_release(
            job_human_id=hid,
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
        )

    def revalidate_blocked(
        self,
        job_human_id: str | None,
        *,
        projectctl_runner=None,
        show_work_item_fn=None,
    ) -> RevalidateBlockedResult:
        hid = _require_job_id(job_human_id, "--revalidate-blocked")
        return revalidate_blocked_job(
            job_human_id=hid,
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            projectctl_runner=projectctl_runner,
            show_work_item_fn=show_work_item_fn,
        )

    def reclaim_running(
        self,
        job_human_id: str | None,
        *,
        promote: bool = True,
    ) -> ReclaimRunningResult:
        hid = _require_job_id(job_human_id, "--reclaim-running")
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            before = get_job_by_human_id(conn, hid)
            if before is None:
                raise OrchestrationError(f"job {hid!r} not found")
            job = reclaim_interrupted_running_job(
                conn,
                hid,
                reason=(
                    "governed reclaim after interrupted Cursor worker "
                    "(Ctrl+C / hang)"
                ),
            )
            if job.status == "RETRY_WAIT" and promote:
                job = promote_retry_wait_to_ready(
                    conn,
                    job.id,
                    reason="recovery: reclaimed interrupted RUNNING -> READY",
                )
        return ReclaimRunningResult(
            job_human_id=before.human_id,
            status_before=before.status,
            status_after=job.status,
            attempt=job.attempt,
            last_error=job.last_error,
            base_git_sha=before.base_git_sha,
            worktree_path=before.worktree_path,
        )

    def reconcile_fat(
        self,
        project_human_id: str,
        iteration_human_id: str,
        *,
        skip_work_items: bool = False,
        projectctl_runner=None,
    ) -> FatReconcileResult:
        if project_human_id != "PRJ-003" or iteration_human_id != "ITER-002":
            raise OrchestrationError(
                "only PRJ-003 / ITER-002 FAT reconcile is implemented"
            )
        return reconcile_prj003_iter002_fat(
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            projectctl_runner=projectctl_runner,
            ensure_work_items=not skip_work_items,
        )


class IterationService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def run(
        self,
        project_human_id: str,
        *,
        iteration_human_id: str | None = None,
        dry_run: bool = False,
        max_parallel: int = 3,
        cursor_runner: Callable[..., Any] | None = None,
        projectctl_runner=None,
        plan_override: dict[str, Any] | None = None,
        skip_identity_validation: bool = False,
    ) -> IterationResult:
        return run_iteration(
            project_human_id=project_human_id,
            iteration_human_id=iteration_human_id,
            dry_run=dry_run,
            max_parallel=max_parallel,
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            cursor_runner=cursor_runner,
            projectctl_runner=projectctl_runner,
            plan_override=plan_override,
            skip_identity_validation=skip_identity_validation,
        )


class StatusService:
    """Daemon, job, and delivery-repo status queries."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def daemon(self) -> DaemonStatus:
        return get_daemon_status(self.ctx.db_path)

    def job(self, job_human_id: str) -> OrchestrationJob:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            job = get_job_by_human_id(conn, job_human_id)
        if job is None:
            raise OrchestrationError(f"job {job_human_id!r} not found")
        return job

    def delivery(
        self,
        project_human_id: str,
        *,
        claimed_repository_root: Path | str | None = None,
        projectctl_runner=None,
    ) -> ProjectctlStatusResult:
        project = self.ctx.resolve_project(
            project_human_id,
            claimed_repository_root=claimed_repository_root,
            projectctl_runner=projectctl_runner,
        )
        runner = projectctl_runner or run_projectctl_status
        return runner(project.git_root)

    def jobs_for_project(
        self,
        project_human_id: str,
        *,
        statuses: frozenset[str] | set[str] | None = None,
    ) -> list[OrchestrationJob]:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return list_jobs_for_project(
                conn, project_human_id, statuses=statuses
            )


@dataclass(frozen=True)
class ProjectSummary:
    project_human_id: str
    enabled: bool
    job_counts: dict[str, int]
    current_iteration_human_id: str | None
    current_release_job_human_id: str | None
    current_release_status: str | None
    has_accepted_plan: bool


@dataclass(frozen=True)
class CurrentIterationRelease:
    project_human_id: str
    iteration_human_id: str | None
    release_job_human_id: str | None
    release_status: str | None
    from_accepted_plan: bool


@dataclass(frozen=True)
class JobGraph:
    nodes: list[OrchestrationJob]
    edges: list[tuple[str, str]]


class ProjectQueryService:
    """Project-scoped inspection. Identity is always project_human_id."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def _require_project(self, project_human_id: str) -> RegistryEntry:
        return RegistryService(self.ctx).show(project_human_id)

    def summary(self, project_human_id: str) -> ProjectSummary:
        entry = self._require_project(project_human_id)
        current = self.current(project_human_id)
        jobs = StatusService(self.ctx).jobs_for_project(project_human_id)
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            accepted = load_latest_accepted_plan(conn, project_human_id)
        return ProjectSummary(
            project_human_id=entry.project_human_id,
            enabled=bool(entry.enabled),
            job_counts=counts,
            current_iteration_human_id=current.iteration_human_id,
            current_release_job_human_id=current.release_job_human_id,
            current_release_status=current.release_status,
            has_accepted_plan=accepted is not None,
        )

    def current(self, project_human_id: str) -> CurrentIterationRelease:
        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            accepted = load_latest_accepted_plan(conn, project_human_id)
            jobs = list_jobs_for_project(conn, project_human_id)
        iteration = None
        from_plan = False
        if accepted and accepted.get("iteration_human_id"):
            iteration = str(accepted.get("iteration_human_id"))
            from_plan = True
        if iteration is None:
            for job in reversed(jobs):
                if job.iteration_human_id:
                    iteration = job.iteration_human_id
                    break
        release_jobs = [j for j in jobs if j.queue == "RELEASE"]
        release = release_jobs[-1] if release_jobs else None
        return CurrentIterationRelease(
            project_human_id=project_human_id,
            iteration_human_id=iteration,
            release_job_human_id=release.human_id if release else None,
            release_status=release.status if release else None,
            from_accepted_plan=from_plan,
        )

    def jobs(self, project_human_id: str) -> list[OrchestrationJob]:
        self._require_project(project_human_id)
        return StatusService(self.ctx).jobs_for_project(project_human_id)

    def job(self, project_human_id: str, job_human_id: str) -> OrchestrationJob:
        self._require_project(project_human_id)
        job = StatusService(self.ctx).job(job_human_id)
        if job.project_human_id != project_human_id:
            raise OrchestrationError(f"job {job_human_id!r} not found")
        return job

    def graph(self, project_human_id: str) -> JobGraph:
        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            nodes, edges = list_dependency_graph_for_project(conn, project_human_id)
        return JobGraph(nodes=nodes, edges=edges)

    def dispatch_eligible(self, project_human_id: str) -> list[OrchestrationJob]:
        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return list_eligible_ready_jobs(
                conn, project_human_id=project_human_id
            )

    def recent_events(
        self, project_human_id: str, *, limit: int = 50
    ) -> list[RunEventRecord]:
        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return list_run_events_for_project(
                conn, project_human_id, limit=limit
            )

    def agent_runs(
        self, project_human_id: str, *, limit: int = 40
    ) -> list[dict]:
        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return list_agent_runs_for_project(conn, project_human_id, limit=limit)

    def quality(self, project_human_id: str) -> dict[str, Any]:
        from projectos.services.quality import build_quality_snapshot

        jobs = self.jobs(project_human_id)
        graph = self.graph(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            evidence = list_assurance_for_project(conn, project_human_id)
            invalidations = list_invalidations_for_project(conn, project_human_id)
        return build_quality_snapshot(
            project_human_id=project_human_id,
            jobs=jobs,
            evidence=evidence,
            invalidations=invalidations,
            edges=graph.edges,
        )

    def releases(self, project_human_id: str) -> dict[str, Any]:
        from projectos.services.releases import build_release_list

        jobs = self.jobs(project_human_id)
        quality = self.quality(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            integrations = list_integrations_for_project(conn, project_human_id)
            counts = {
                job.human_id: len(list_release_artifacts(conn, project_human_id, job.human_id))
                for job in jobs
                if job.queue == "RELEASE"
            }
        return build_release_list(
            project_human_id=project_human_id,
            jobs=jobs,
            integrations=integrations,
            quality=quality,
            artifacts_by_release=counts,
        )

    def release(self, project_human_id: str, release_human_id: str) -> dict[str, Any]:
        from projectos.services.releases import build_release_detail

        jobs = self.jobs(project_human_id)
        quality = self.quality(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            integrations = list_integrations_for_project(conn, project_human_id)
            return build_release_detail(
                conn,
                project_human_id=project_human_id,
                release_human_id=release_human_id,
                jobs=jobs,
                integrations=integrations,
                quality=quality,
            )

    def release_artifact(
        self,
        project_human_id: str,
        release_human_id: str,
        artifact_human_id: str,
    ) -> dict[str, Any]:
        from projectos.services.releases import load_release_artifact_bytes

        self._require_project(project_human_id)
        jobs = self.jobs(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return load_release_artifact_bytes(
                conn,
                project_human_id=project_human_id,
                release_human_id=release_human_id,
                artifact_human_id=artifact_human_id,
                jobs=jobs,
            )

    def report_catalog(self, project_human_id: str) -> dict[str, Any]:
        from projectos.services.reporting import build_report_catalog

        self._require_project(project_human_id)
        return build_report_catalog(project_human_id)

    def report(
        self,
        project_human_id: str,
        kind: str,
        *,
        iteration_human_id: str | None = None,
    ) -> dict[str, Any]:
        from projectos.services.reporting import collect_report

        entry = self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return collect_report(
                conn,
                project_human_id=project_human_id,
                kind=kind,
                enabled=bool(entry.enabled),
                iteration_human_id=iteration_human_id,
            )

    def report_download(
        self,
        project_human_id: str,
        kind: str,
        *,
        fmt: str,
        iteration_human_id: str | None = None,
    ) -> dict[str, Any]:
        from projectos.services.report_render import render_report_download

        report = self.report(
            project_human_id, kind, iteration_human_id=iteration_human_id
        )
        return render_report_download(report, fmt)

    def report_dashboard(
        self,
        project_human_id: str,
        *,
        iteration_human_id: str | None = None,
    ) -> dict[str, Any]:
        from projectos.services.reporting import collect_report_dashboard

        entry = self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return collect_report_dashboard(
                conn,
                project_human_id=project_human_id,
                enabled=bool(entry.enabled),
                iteration_human_id=iteration_human_id,
            )

    def report_snapshots(self, project_human_id: str) -> dict[str, Any]:
        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return {
                "origin": "snapshot",
                "project_human_id": project_human_id,
                "snapshots": list_report_snapshots(conn, project_human_id),
            }

    def report_snapshot(self, project_human_id: str, snapshot_human_id: str) -> dict[str, Any]:
        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            row = get_report_snapshot(
                conn,
                project_human_id=project_human_id,
                snapshot_human_id=snapshot_human_id,
            )
        if row is None:
            raise OrchestrationError(f"snapshot {snapshot_human_id!r} not found")
        return row

    def save_report_snapshot(
        self,
        project_human_id: str,
        kind: str,
        *,
        iteration_human_id: str | None = None,
    ) -> dict[str, Any]:
        report = self.report(
            project_human_id, kind, iteration_human_id=iteration_human_id
        )
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return upsert_report_snapshot(conn, report)

    def report_snapshot_download(
        self,
        project_human_id: str,
        snapshot_human_id: str,
        *,
        fmt: str,
    ) -> dict[str, Any]:
        from projectos.services.report_render import render_report_download

        envelope = self.report_snapshot(project_human_id, snapshot_human_id)
        return render_report_download(envelope, fmt)

    def learning(self, project_human_id: str) -> dict[str, Any]:
        from projectos.learning import learning_view

        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return learning_view(conn, project_human_id)


class MemoryAdminService:
    """Governed retire/supersede. Confirmation, reason, and actor are required."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def _require_project(self, project_human_id: str):
        return RegistryService(self.ctx).show(project_human_id)

    def retire(
        self,
        project_human_id: str,
        memory_human_id: str,
        *,
        confirmed: bool,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        from projectos.learning import retire_memory

        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return retire_memory(
                conn,
                project_human_id=project_human_id,
                memory_human_id=memory_human_id,
                confirmed=confirmed,
                reason=reason,
                actor=actor,
            )

    def supersede(
        self,
        project_human_id: str,
        memory_human_id: str,
        *,
        successor_title: str,
        confirmed: bool,
        reason: str,
        actor: str,
        evidence_ref: str | None = None,
    ) -> dict[str, Any]:
        from projectos.learning import supersede_memory

        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return supersede_memory(
                conn,
                project_human_id=project_human_id,
                memory_human_id=memory_human_id,
                successor_title=successor_title,
                confirmed=confirmed,
                reason=reason,
                actor=actor,
                evidence_ref=evidence_ref,
            )


class ReleaseService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def evaluate(self, job_human_id: str, *, ops=None) -> ReleaseEvaluation:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            job = get_job_by_human_id(conn, job_human_id)
            if job is None:
                raise OrchestrationError(f"job {job_human_id!r} not found")
            workspace = Path(job.repository_root)
            return evaluate_release_job(
                conn,
                job,
                workspace=workspace,
                registry_path=self.ctx.registry_path,
                ops=ops,
            )

    def assemble_qa_package(
        self,
        job_human_id: str,
        *,
        expected_integration_sha: str,
        evidence_dir: Path,
        required_story_shas: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], list[str], list]:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            job = get_job_by_human_id(conn, job_human_id)
            if job is None:
                raise OrchestrationError(f"job {job_human_id!r} not found")
            return assemble_qa_package(
                conn,
                job,
                expected_integration_sha=expected_integration_sha,
                evidence_dir=evidence_dir,
                required_story_shas=required_story_shas,
            )


@dataclass(frozen=True)
class JobLearningRecord:
    job_human_id: str
    queue: str
    role: str
    status: str
    outcome: str | None
    work_item_human_id: str | None
    candidate_git_sha: str | None


@dataclass(frozen=True)
class AgentRunLearningRecord:
    job_human_id: str
    role: str
    exit_code: int | None
    duration_ms: int | None
    error: str | None
    candidate_git_sha: str | None
    usage_json: str | None


class LearningService:
    """Read-only job/run history for later learning — no writes."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def job_history(
        self,
        project_human_id: str,
        *,
        statuses: frozenset[str] | set[str] | None = None,
    ) -> list[JobLearningRecord]:
        jobs = StatusService(self.ctx).jobs_for_project(
            project_human_id, statuses=statuses
        )
        return [
            JobLearningRecord(
                job_human_id=j.human_id,
                queue=j.queue,
                role=j.agent_role,
                status=j.status,
                outcome=j.outcome,
                work_item_human_id=j.work_item_human_id,
                candidate_git_sha=j.candidate_git_sha,
            )
            for j in jobs
        ]

    def agent_runs(self, project_human_id: str) -> list[AgentRunLearningRecord]:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            rows = conn.execute(
                """
                SELECT j.human_id, j.agent_role, r.exit_code, r.duration_ms,
                       r.error, j.candidate_git_sha, r.usage_json
                FROM agent_runs r
                JOIN orchestration_jobs j ON j.id = r.job_id
                WHERE j.project_human_id = ?
                ORDER BY r.id ASC
                """,
                (project_human_id,),
            ).fetchall()
        return [
            AgentRunLearningRecord(
                job_human_id=str(r["human_id"]),
                role=str(r["agent_role"]),
                exit_code=int(r["exit_code"]) if r["exit_code"] is not None else None,
                duration_ms=int(r["duration_ms"]) if r["duration_ms"] is not None else None,
                error=r["error"],
                candidate_git_sha=r["candidate_git_sha"],
                usage_json=r["usage_json"],
            )
            for r in rows
        ]


class ReportingService:
    """Budget, doctor, and schedule inputs for operators/reports."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def budget(
        self,
        project_human_id: str,
        *,
        iteration_human_id: str | None = None,
    ) -> BudgetReport:
        return build_budget_report(
            project_human_id=project_human_id,
            iteration_human_id=iteration_human_id,
            db_path=self.ctx.db_path,
        )

    def doctor(self, *, projectctl_runner=None) -> DoctorReport:
        return run_doctor(
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            projectctl_runner=projectctl_runner,
        )

    def list_schedules(self):
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return list_schedules(conn)

    def catalog(self, project_human_id: str) -> dict[str, Any]:
        return ProjectQueryService(self.ctx).report_catalog(project_human_id)

    def collect(
        self,
        project_human_id: str,
        kind: str,
        *,
        iteration_human_id: str | None = None,
    ) -> dict[str, Any]:
        return ProjectQueryService(self.ctx).report(
            project_human_id, kind, iteration_human_id=iteration_human_id
        )

    def render(self, report: dict[str, Any], *, fmt: str = "markdown") -> dict[str, Any] | str:
        from projectos.services.report_render import render_report_download, render_report_markdown

        if fmt == "markdown":
            return render_report_markdown(report)
        return render_report_download(report, fmt)

    def download(
        self,
        project_human_id: str,
        kind: str,
        *,
        fmt: str = "html",
        iteration_human_id: str | None = None,
    ) -> dict[str, Any]:
        return ProjectQueryService(self.ctx).report_download(
            project_human_id,
            kind,
            fmt=fmt,
            iteration_human_id=iteration_human_id,
        )

    def evaluate_due(self, *, clock=None, projectctl_runner=None) -> ScheduleReport:
        return evaluate_due(
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            clock=clock,
            projectctl_runner=projectctl_runner,
        )

    def upsert_schedule(
        self,
        project_human_id: str,
        *,
        enabled: bool = True,
        timezone: str = "UTC",
        cadence: str = "daily",
        local_time: str = "09:00",
        approved_budget_tokens: int | None = None,
    ) -> None:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            upsert_schedule(
                conn,
                project_human_id=project_human_id,
                enabled=enabled,
                timezone=timezone,
                cadence=cadence,
                local_time=local_time,
                approved_budget_tokens=approved_budget_tokens,
            )


class ApprovalService:
    """Sponsor-authority and release-gate queries. Never auto-approves governance."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def plan_errors(
        self,
        plan: dict[str, Any],
        *,
        expected_project_id: str,
        known_work_items: dict[str, set[str]] | None = None,
    ) -> list[str]:
        return validate_plan_document(
            plan,
            expected_project_id=expected_project_id,
            known_work_items=known_work_items,
        )

    def sponsor_granted(
        self,
        plan: dict[str, Any],
        *,
        expected_project_id: str,
    ) -> bool:
        errors = self.plan_errors(plan, expected_project_id=expected_project_id)
        return not any("Sponsor-authority" in e for e in errors)

    def release_gate(self, job_human_id: str, *, ops=None) -> ReleaseEvaluation:
        return ReleaseService(self.ctx).evaluate(job_human_id, ops=ops)

    def _require_project(self, project_human_id: str):
        return RegistryService(self.ctx).show(project_human_id)

    def list_decisions(self, project_human_id: str, *, status: str | None = None) -> dict[str, Any]:
        from projectos.decisions import list_decisions

        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return list_decisions(conn, project_human_id, status=status)

    def get_decision(self, project_human_id: str, decision_human_id: str) -> dict[str, Any]:
        from projectos.decisions import get_decision

        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return get_decision(conn, project_human_id, decision_human_id)

    def open_decision(
        self,
        project_human_id: str,
        *,
        action: str,
        reason: str,
        impact: str,
        requested_by: str,
        target_kind: str = "none",
        target_human_id: str | None = None,
    ) -> dict[str, Any]:
        from projectos.decisions import open_decision

        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return open_decision(
                conn,
                project_human_id=project_human_id,
                action=action,
                reason=reason,
                impact=impact,
                requested_by=requested_by,
                target_kind=target_kind,
                target_human_id=target_human_id,
            )

    def approve_decision(
        self,
        project_human_id: str,
        decision_human_id: str,
        *,
        confirmed: bool,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        from projectos.decisions import approve_decision

        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return approve_decision(
                conn,
                project_human_id=project_human_id,
                decision_human_id=decision_human_id,
                confirmed=confirmed,
                actor=actor,
                reason=reason,
            )

    def reject_decision(
        self,
        project_human_id: str,
        decision_human_id: str,
        *,
        confirmed: bool,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        from projectos.decisions import reject_decision

        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return reject_decision(
                conn,
                project_human_id=project_human_id,
                decision_human_id=decision_human_id,
                confirmed=confirmed,
                actor=actor,
                reason=reason,
            )



class SlackBindingService:
    """Bind Slack locations to registered projects. Slack is not project state."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def _require_project(self, project_human_id: str):
        entry = RegistryService(self.ctx).show(project_human_id)
        if not entry.enabled:
            raise OrchestrationError(f"project {project_human_id!r} is disabled")
        return entry

    def list_bindings(self, project_human_id: str) -> dict[str, Any]:
        from projectos.slack import list_bindings

        entry = self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            payload = list_bindings(conn, project_human_id)
        payload["repository_root"] = str(entry.repository_root)
        payload["repository_source"] = "registry"
        return payload

    def bind(
        self,
        project_human_id: str,
        *,
        channel_id: str,
        team_id: str | None = None,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        from projectos.slack import bind_channel

        entry = self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            row = bind_channel(
                conn,
                project_human_id=project_human_id,
                channel_id=channel_id,
                team_id=team_id,
                thread_ts=thread_ts,
            )
        row["repository_root"] = str(entry.repository_root)
        row["repository_source"] = "registry"
        row["notice"] = (
            "Slack identifiers are integration metadata. "
            "Project identity is the registry."
        )
        return row

    def unbind(
        self,
        project_human_id: str,
        *,
        channel_id: str,
        team_id: str | None = None,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        from projectos.slack import unbind_channel

        self._require_project(project_human_id)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            return unbind_channel(
                conn,
                project_human_id=project_human_id,
                channel_id=channel_id,
                team_id=team_id,
                thread_ts=thread_ts,
            )

    def inbound(
        self,
        *,
        channel_id: str,
        team_id: str | None = None,
        thread_ts: str | None = None,
        message_ts: str | None = None,
        project_human_id: str | None = None,
        work_request: dict[str, str] | None = None,
        cursor_runner=None,
        projectctl_runner=None,
    ) -> dict[str, Any]:
        from projectos.intake import IntakeService
        from projectos.slack import NOTICE, resolve_inbound

        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            resolved = resolve_inbound(
                conn,
                channel_id=channel_id,
                team_id=team_id,
                thread_ts=thread_ts,
                message_ts=message_ts,
                project_human_id=project_human_id,
            )
        entry = self._require_project(resolved["project_human_id"])
        resolved["repository_root"] = str(entry.repository_root)
        resolved["repository_source"] = "registry"
        resolved["enabled"] = bool(entry.enabled)
        resolved["notice"] = NOTICE
        resolved["intake"] = None
        if work_request:
            preview = IntakeService(self.ctx).preview(
                resolved["project_human_id"],
                business_request=work_request.get("business_request") or "",
                objective=work_request.get("objective") or "",
                acceptance=work_request.get("acceptance") or "",
                cursor_runner=cursor_runner,
                projectctl_runner=projectctl_runner,
            )
            resolved["intake"] = preview.as_dict()
        return resolved

    def command(
        self,
        *,
        command: str,
        channel_id: str,
        team_id: str | None = None,
        thread_ts: str | None = None,
        message_ts: str | None = None,
        project_human_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        source: str | None = None,
        create_defect_fn=None,
        create_feedback_fn=None,
    ) -> dict[str, Any]:
        from projectos.slack_commands import run_command

        return run_command(
            self.ctx,
            command=command,
            channel_id=channel_id,
            team_id=team_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            project_human_id=project_human_id,
            title=title,
            description=description,
            source=source,
            create_defect_fn=create_defect_fn,
            create_feedback_fn=create_feedback_fn,
        )

    def notify(self, project_human_id: str, *, poster=None) -> dict[str, Any]:
        from projectos.slack_notify import post_due_notifications

        self._require_project(project_human_id)
        return post_due_notifications(self.ctx, project_human_id, poster=poster)

    def list_notifications(self, project_human_id: str) -> dict[str, Any]:
        from projectos.slack_notify import list_notifications

        return list_notifications(self.ctx, project_human_id)


class DaemonService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def status(self) -> DaemonStatus:
        return get_daemon_status(self.ctx.db_path)

    def run(
        self,
        *,
        poll_seconds: float = 5.0,
        max_loops: int | None = None,
        **kwargs: Any,
    ) -> int:
        return run_daemon(
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            poll_seconds=poll_seconds,
            max_loops=max_loops,
            **kwargs,
        )

    def stop(self) -> int:
        return stop_daemon(self.ctx.db_path)
