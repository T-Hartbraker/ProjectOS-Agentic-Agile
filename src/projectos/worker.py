"""ProjectOS worker runtime: lease, execute, persist, release."""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from projectos.constants import ASSURANCE_QUEUES, CODE_MODIFYING_ROLES
from projectos.cursor_adapter import CursorRunResult, invoke_cursor_agent
from projectos.db import connection
from projectos.cockpit_worker import emit_worker_cockpit_event, emit_worker_terminal_event, record_worker_failure
from projectos.delivery_evidence import (
    evaluate_delivery_candidate,
    is_valid_qa_candidate,
)
from projectos.errors import (
    LeaseError,
    OrchestrationError,
    ProjectOSError,
    WorktreeError,
)
from projectos.migrate import initialize_database
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.projectctl_bridge import create_defect, resolve_validated_repo
from projectos.prompt_builder import build_role_prompt, resolve_delivery_assignment
from projectos.qa_handoff import maybe_handoff_after_delivery, process_assurance_worker_success
from projectos.release_provenance import (
    bind_dependent_release_jobs,
    bind_release_provenance,
    promote_eligible_release_jobs,
)
from projectos.registry import load_registry
from projectos.store import (
    OrchestrationJob,
    acquire_lease,
    active_lease_for_job,
    find_active_worktree_holder,
    get_job,
    get_job_by_human_id,
    insert_agent_run,
    mark_running,
    mark_succeeded,
    recover_expired_leases,
    select_ready_job,
    utc_now_iso,
    update_job_worktree,
)
from projectos.validation import validate_registry_entry
from projectos.worktree import (
    build_worktree_name,
    checkout_sha,
    commit_all_changes,
    current_head_sha,
    ensure_worktree,
    is_dirty,
    sha_belongs_to_repo,
)


@dataclass(frozen=True)
class WorkerResult:
    status: str
    job_human_id: str | None
    message: str
    exit_code: int = 0


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _job_requires_worktree(job: OrchestrationJob) -> bool:
    if job.requires_worktree:
        return True
    if job.queue == "RELEASE":
        return True
    return job.agent_role.upper() in CODE_MODIFYING_ROLES


def _revalidate_job_identity(
    job: OrchestrationJob,
    *,
    registry_path: Path,
    projectctl_runner=None,
) -> Any:
    registry = load_registry(registry_path)
    entry = registry.get(job.project_human_id)
    if entry is None:
        raise OrchestrationError(
            f"Project {job.project_human_id} is not in the registry"
        )
    if Path(entry.repository_root).resolve() != Path(job.repository_root).resolve():
        raise OrchestrationError(
            "Repository identity changed after job creation: "
            f"job has {job.repository_root}, registry has {entry.repository_root}"
        )
    validated = validate_registry_entry(entry, projectctl_runner=projectctl_runner)
    if validated.identity.project_human_id != job.project_human_id:
        raise OrchestrationError(
            "Project identity changed after job creation: "
            f"expected {job.project_human_id}, found "
            f"{validated.identity.project_human_id}"
        )
    if job.identity_snapshot_json:
        try:
            snap = json.loads(job.identity_snapshot_json)
            snap_root = snap.get("repository_root")
            if snap_root and Path(snap_root).resolve() != Path(
                job.repository_root
            ).resolve():
                raise OrchestrationError(
                    "Stored identity snapshot repository_root mismatch"
                )
        except json.JSONDecodeError:
            pass
    return validated


def run_once(
    *,
    db_path: Path | str | None = None,
    registry_path: Path | str | None = None,
    queue: str | None = None,
    role: str | None = None,
    job_human_id: str | None = None,
    lease_seconds: int = 900,
    timeout_seconds: float = 1800.0,
    worker_id: str | None = None,
    cursor_runner: Callable[..., Any] | None = None,
    projectctl_runner=None,
    skip_identity_validation: bool = False,
    cancel_event=None,
    release_evaluator=None,
) -> WorkerResult:
    """Select one READY job, execute it, and persist outcomes.

    SQLite connections are never held open while Cursor runs.
    """
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    reg_path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    wid = worker_id or default_worker_id()
    initialize_database(path)
    injected_memories: list = []

    # --- Phase 1: select, lease, mark running, prepare worktree (short TX) ---
    with connection(path) as conn:
        recover_expired_leases(conn)

        if job_human_id:
            existing = get_job_by_human_id(conn, job_human_id)
            if existing is None:
                return WorkerResult(
                    status="error",
                    job_human_id=job_human_id,
                    message=f"Job {job_human_id!r} not found",
                    exit_code=1,
                )
            if existing.status != "READY":
                return WorkerResult(
                    status="skipped",
                    job_human_id=job_human_id,
                    message=f"Job {job_human_id} is {existing.status}, not READY",
                    exit_code=0,
                )
            job = existing
        else:
            selected = select_ready_job(
                conn, queue=queue, role=role, job_human_id=None
            )
            if selected is None:
                return WorkerResult(
                    status="idle",
                    job_human_id=None,
                    message="No READY jobs",
                    exit_code=0,
                )
            job = selected

        try:
            acquire_lease(conn, job, worker_id=wid, lease_seconds=lease_seconds)
        except LeaseError as exc:
            return WorkerResult(
                status="lease_failed",
                job_human_id=job.human_id,
                message=str(exc),
                exit_code=1,
            )

        job = get_job(conn, job.id)
        try:
            if not skip_identity_validation:
                _revalidate_job_identity(
                    job,
                    registry_path=reg_path,
                    projectctl_runner=projectctl_runner,
                )
        except ProjectOSError as exc:
            record_worker_failure(conn, job.id, error=str(exc), blocked=True)
            return WorkerResult(
                status="blocked",
                job_human_id=job.human_id,
                message=str(exc),
                exit_code=1,
            )

        job = mark_running(conn, job.id)
        emit_worker_cockpit_event(
            conn,
            job,
            event_type="WORK_STARTED",
            summary=f"{job.human_id} started in {job.queue} queue.",
            detail_level="normal",
        )

        if job.queue == "RELEASE":
            try:
                job = bind_release_provenance(conn, job)
            except OrchestrationError as exc:
                record_worker_failure(conn, job.id, error=str(exc), blocked=True)
                return WorkerResult(
                    status="blocked",
                    job_human_id=job.human_id,
                    message=str(exc),
                    exit_code=1,
                )

        workspace = Path(job.repository_root)
        worktree_name = job.worktree_name
        worktree_path: Path | None = (
            Path(job.worktree_path) if job.worktree_path else None
        )
        base_sha = job.base_git_sha

        try:
            if _job_requires_worktree(job):
                name = worktree_name or build_worktree_name(
                    job.project_human_id,
                    iteration_human_id=job.iteration_human_id,
                    job_human_id=job.human_id,
                )
                holder = find_active_worktree_holder(
                    conn, name, exclude_job_id=job.id
                )
                if holder is not None:
                    raise WorktreeError(
                        f"Worktree {name!r} already claimed by active job "
                        f"{holder.human_id} ({holder.status})"
                    )
                if job.queue == "RELEASE":
                    base_ref = job.source_candidate_sha or job.base_git_sha or "HEAD"
                else:
                    base_ref = (
                        job.base_git_sha
                        or job.source_candidate_sha
                        or "HEAD"
                    )
                info = ensure_worktree(
                    Path(job.repository_root),
                    name=name,
                    path=worktree_path,
                    base_ref=base_ref,
                )
                worktree_name = info.name
                worktree_path = info.path
                base_sha = info.base_sha
                workspace = info.path
                if (
                    job.queue == "RELEASE"
                    and job.source_candidate_sha
                    and current_head_sha(workspace) != job.source_candidate_sha
                ):
                    checkout_sha(workspace, job.source_candidate_sha)
                    base_sha = job.source_candidate_sha
                job = update_job_worktree(
                    conn,
                    job.id,
                    worktree_name=info.name,
                    worktree_path=str(info.path),
                    base_git_sha=base_sha,
                )
            else:
                base_sha = base_sha or current_head_sha(Path(job.repository_root))
        except (WorktreeError, ProjectOSError) as exc:
            record_worker_failure(conn, job.id, error=str(exc), blocked=True)
            return WorkerResult(
                status="blocked",
                job_human_id=job.human_id,
                message=str(exc),
                exit_code=1,
            )

        resolved = None
        projectctl_python = None
        if job.queue == "DELIVERY":
            try:
                if not skip_identity_validation:
                    validated = resolve_validated_repo(
                        job.project_human_id,
                        registry_path=reg_path,
                        projectctl_runner=projectctl_runner,
                    )
                    projectctl_python = validated.projectctl_python
                    repo_for_wi = validated.git_root
                else:
                    repo_for_wi = Path(job.repository_root)
                resolved = resolve_delivery_assignment(
                    job,
                    repository_root=repo_for_wi,
                    python_executable=projectctl_python,
                )
            except (OrchestrationError, ProjectOSError) as exc:
                record_worker_failure(conn, job.id, error=str(exc), blocked=True)
                return WorkerResult(
                    status="blocked",
                    job_human_id=job.human_id,
                    message=str(exc),
                    exit_code=1,
                )

        from projectos.learning import format_memory_context, list_active_memories_for_prompt

        injected_memories = list_active_memories_for_prompt(
            conn, job.project_human_id, job.agent_role
        )
        prompt = build_role_prompt(
            job,
            workspace_path=str(workspace),
            base_git_sha=base_sha,
            extra_context=format_memory_context(injected_memories),
            resolved=resolved,
        )
        job_id = job.id
        job_human = job.human_id
        job_queue = job.queue
        job_repo = job.repository_root
        job_role = job.agent_role
        source_delivery_job_id = job.source_delivery_job_id
        source_candidate_sha = job.source_candidate_sha
        allows_no_change = job.allows_no_change
        job_project = job.project_human_id

    # --- Phase 2: execute without holding SQLite ---
    cursor_result: CursorRunResult | None = None
    run_error: str | None = None
    candidate_sha: str | None = None
    dirty: bool | None = None
    release_eval = None
    try:
        evidence_root = (
            worktree_path
            if worktree_path is not None and worktree_path.exists()
            else Path(job_repo)
        )
        if job_queue == "RELEASE":
            from projectos.release_readiness import evaluate_release_job

            evaluator = release_evaluator or evaluate_release_job
            with connection(path) as gate_conn:
                job_now = get_job(gate_conn, job_id)
                release_eval = evaluator(
                    gate_conn,
                    job_now,
                    workspace=evidence_root,
                    registry_path=reg_path,
                )
            candidate_sha = current_head_sha(evidence_root)
            dirty = is_dirty(evidence_root)
            now = utc_now_iso()
            cursor_result = CursorRunResult(
                command=["projectos-release-gate"],
                returncode=0,
                stdout=release_eval.readiness_report_path.read_text(encoding="utf-8"),
                stderr="\n".join(release_eval.reasons),
                started_at=now,
                ended_at=now,
                duration_ms=0,
                output_ref=str(release_eval.readiness_report_path),
                stdout_ref=str(release_eval.readiness_report_path),
                stderr_ref=str(release_eval.readiness_report_path),
                prompt_ref=None,
                workspace=evidence_root,
                worktree_name=worktree_name,
                usage={"status": "gate", "approved": release_eval.approved},
            )
        else:
            cursor_result = invoke_cursor_agent(
                prompt=prompt,
                workspace=workspace,
                run_id=f"{job_human}-{uuid.uuid4().hex[:8]}",
                timeout_seconds=timeout_seconds,
                worktree_name=None,
                runner=cursor_runner,
                cancel_event=cancel_event,
                force=True,
                trust=True,
            )
            if job_queue == "DELIVERY" and worktree_path is not None:
                if is_dirty(worktree_path):
                    candidate_sha = commit_all_changes(
                        worktree_path,
                        f"projectos: delivery candidate for {job_human}",
                    )
                else:
                    candidate_sha = current_head_sha(worktree_path)
                dirty = is_dirty(worktree_path)
            else:
                candidate_sha = current_head_sha(evidence_root)
                dirty = is_dirty(evidence_root)
    except Exception as exc:  # noqa: BLE001
        run_error = str(exc)

    # --- Phase 3: persist results (short TX) ---
    with connection(path) as conn:
        run_id = insert_agent_run(
            conn,
            job_id=job_id,
            worker_id=wid,
            cursor_command=cursor_result.command if cursor_result else [],
            prompt_ref=cursor_result.prompt_ref if cursor_result else None,
            output_ref=cursor_result.output_ref if cursor_result else None,
            stdout_ref=cursor_result.stdout_ref if cursor_result else None,
            stderr_ref=cursor_result.stderr_ref if cursor_result else None,
            exit_code=cursor_result.returncode if cursor_result else None,
            started_at=cursor_result.started_at if cursor_result else None,
            ended_at=cursor_result.ended_at if cursor_result else None,
            duration_ms=cursor_result.duration_ms if cursor_result else None,
            worktree_name=worktree_name,
            worktree_path=str(worktree_path) if worktree_path else None,
            base_git_sha=base_sha,
            candidate_git_sha=candidate_sha,
            dirty=dirty,
            usage=cursor_result.usage if cursor_result else None,
            error=run_error
            or (
                None
                if cursor_result
                and cursor_result.returncode == 0
                and not cursor_result.timed_out
                and not cursor_result.cancelled
                else (
                    "cursor cancelled"
                    if cursor_result and cursor_result.cancelled
                    else (
                        "cursor timed out"
                        if cursor_result and cursor_result.timed_out
                        else (
                            f"cursor exit {cursor_result.returncode}"
                            if cursor_result
                            else run_error
                        )
                    )
                )
            ),
        )
        if injected_memories:
            from projectos.learning import record_injections

            record_injections(
                conn,
                project_human_id=job_project,
                job_human_id=job_human,
                agent_run_id=run_id,
                memories=injected_memories,
            )

        if run_error or cursor_result is None:
            err = run_error or "cursor adapter returned no result"
            final = record_worker_failure(
                conn,
                job_id,
                error=err,
                output_ref=cursor_result.output_ref if cursor_result else None,
            )
            return WorkerResult(
                status=final.status.lower(),
                job_human_id=job_human,
                message=err,
                exit_code=1,
            )

        if (
            cursor_result.timed_out
            or cursor_result.cancelled
            or cursor_result.returncode != 0
        ):
            detail = (
                "cursor cancelled"
                if cursor_result.cancelled
                else (
                    f"cursor timed out after {timeout_seconds}s"
                    if cursor_result.timed_out
                    else f"cursor exited {cursor_result.returncode}"
                )
            )
            if cursor_result.stderr.strip():
                detail = f"{detail}: {cursor_result.stderr.strip()[:500]}"
            final = record_worker_failure(
                conn,
                job_id,
                error=detail,
                output_ref=cursor_result.output_ref,
            )
            return WorkerResult(
                status=final.status.lower(),
                job_human_id=job_human,
                message=detail,
                exit_code=1,
            )

        if injected_memories:
            from projectos.learning import reinforce_memories

            reinforce_memories(
                conn, injected_memories, job_human_id=job_human
            )

        if job_queue in ASSURANCE_QUEUES and source_delivery_job_id:
            delivery = get_job(conn, source_delivery_job_id)
            if delivery.outcome in {"INVALIDATED", "SUPERSEDED", "NO_CHANGE"}:
                msg = (
                    f"Source delivery {delivery.human_id} outcome="
                    f"{delivery.outcome}; assurance cannot approve"
                )
                final = record_worker_failure(conn, job_id, error=msg, blocked=True)
                return WorkerResult(
                    status="blocked",
                    job_human_id=job_human,
                    message=msg,
                    exit_code=1,
                )
            if (
                delivery.candidate_git_sha
                and source_candidate_sha
                and delivery.candidate_git_sha != source_candidate_sha
            ):
                msg = (
                    f"Stale QA evidence for {source_candidate_sha}; "
                    f"delivery candidate is {delivery.candidate_git_sha}"
                )
                final = record_worker_failure(conn, job_id, error=msg, blocked=True)
                return WorkerResult(
                    status="blocked",
                    job_human_id=job_human,
                    message=msg,
                    exit_code=1,
                )

        outcome: str | None = None
        if job_queue == "DELIVERY":
            if worktree_path is None:
                err = "DELIVERY requires an isolated worktree"
                final = record_worker_failure(conn, job_id, error=err, blocked=True)
                return WorkerResult(
                    status="blocked",
                    job_human_id=job_human,
                    message=err,
                    exit_code=1,
                )
            if candidate_sha and not sha_belongs_to_repo(worktree_path, candidate_sha):
                err = (
                    f"candidate_git_sha {candidate_sha} is not present in "
                    f"worktree {worktree_path}"
                )
                final = record_worker_failure(conn, job_id, error=err, blocked=True)
                return WorkerResult(
                    status="blocked",
                    job_human_id=job_human,
                    message=err,
                    exit_code=1,
                )
            # Refresh job snapshot for allows_no_change / assignment fields
            job_snap = get_job(conn, job_id)
            evaluation = evaluate_delivery_candidate(
                job_snap,
                base_git_sha=base_sha,
                candidate_git_sha=candidate_sha,
                dirty=dirty,
                cursor_stdout=cursor_result.stdout,
                code_changing=job_role.upper() in CODE_MODIFYING_ROLES,
            )
            if not evaluation.ok:
                final = record_worker_failure(
                    conn,
                    job_id,
                    error=evaluation.error or "invalid delivery candidate",
                    output_ref=cursor_result.output_ref,
                )
                return WorkerResult(
                    status=final.status.lower(),
                    job_human_id=job_human,
                    message=evaluation.error or "invalid delivery candidate",
                    exit_code=1,
                )
            outcome = evaluation.outcome

        if job_queue == "RELEASE":
            if not source_candidate_sha:
                err = (
                    "RELEASE refused: missing integrated candidate provenance"
                )
                final = record_worker_failure(conn, job_id, error=err, blocked=True)
                return WorkerResult(
                    status="blocked",
                    job_human_id=job_human,
                    message=err,
                    exit_code=1,
                )
            if dirty:
                err = (
                    "RELEASE left a dirty worktree; cannot record provenance "
                    f"for integrated candidate {source_candidate_sha}"
                )
                final = record_worker_failure(conn, job_id, error=err, blocked=True)
                return WorkerResult(
                    status="blocked",
                    job_human_id=job_human,
                    message=err,
                    exit_code=1,
                )
            if candidate_sha != source_candidate_sha:
                err = (
                    "RELEASE refused: workspace SHA "
                    f"{candidate_sha} is not integrated candidate "
                    f"{source_candidate_sha}"
                )
                final = record_worker_failure(conn, job_id, error=err, blocked=True)
                return WorkerResult(
                    status="blocked",
                    job_human_id=job_human,
                    message=err,
                    exit_code=1,
                )
            candidate_sha = source_candidate_sha
            if release_eval is not None and not release_eval.approved:
                err = (
                    "RELEASE gate rejected: "
                    + "; ".join(release_eval.reasons)
                )
                final = record_worker_failure(
                    conn,
                    job_id,
                    error=err,
                    blocked=True,
                    output_ref=str(release_eval.readiness_report_path),
                )
                from projectos.domain_events import lookup_event_context_for_job
                from projectos.release_gate_remediation import handle_release_blocked_job

                event_ctx = lookup_event_context_for_job(conn, job_id)
                if event_ctx is not None:
                    try:
                        handle_release_blocked_job(
                            conn,
                            event_ctx=event_ctx,
                            job=final,
                            release_eval=release_eval,
                            repository_root=job_repo,
                        )
                    except Exception:
                        pass
                return WorkerResult(
                    status="blocked",
                    job_human_id=job_human,
                    message=err,
                    exit_code=1,
                )
            outcome = (
                release_eval.outcome
                if release_eval is not None
                else outcome
            )

        final = mark_succeeded(
            conn,
            job_id,
            output_ref=cursor_result.output_ref,
            candidate_git_sha=candidate_sha,
            outcome=outcome,
        )
        emit_worker_terminal_event(
            conn,
            final,
            status="SUCCEEDED",
            outcome=outcome,
        )
        assert active_lease_for_job(conn, job_id) is None

        if final.queue == "DELIVERY" and is_valid_qa_candidate(final):
            maybe_handoff_after_delivery(conn, final)
        elif final.queue == "INTEGRATION":
            bind_dependent_release_jobs(conn, final)
        elif final.queue == "QA_MANAGER" and final.source_candidate_sha:
            from projectos.qa_manager import execute_qa_manager_aggregation

            execute_qa_manager_aggregation(conn, final)
        elif final.queue in ASSURANCE_QUEUES and final.source_candidate_sha:
            process_assurance_worker_success(
                conn,
                final,
                stdout=cursor_result.stdout,
                evidence_ref=cursor_result.output_ref,
                create_defect_fn=create_defect,
            )
            promote_eligible_release_jobs(
                conn,
                project_human_id=final.project_human_id,
                iteration_human_id=final.iteration_human_id,
            )

        return WorkerResult(
            status="succeeded",
            job_human_id=final.human_id,
            message=(
                "Worker task SUCCEEDED (not QA/release acceptance) "
                f"duration_ms={cursor_result.duration_ms}"
                + (f" outcome={outcome}" if outcome else "")
            ),
            exit_code=0,
        )
