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
from projectos.qa_handoff import maybe_handoff_after_delivery, record_assurance_result
from projectos.registry import load_registry
from projectos.store import (
    OrchestrationJob,
    acquire_lease,
    active_lease_for_job,
    find_active_worktree_holder,
    get_job,
    get_job_by_human_id,
    insert_agent_run,
    mark_failure,
    mark_running,
    mark_succeeded,
    recover_expired_leases,
    select_ready_job,
    update_job_worktree,
)
from projectos.validation import validate_registry_entry
from projectos.worktree import (
    build_worktree_name,
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
) -> WorkerResult:
    """Select one READY job, execute it, and persist outcomes.

    SQLite connections are never held open while Cursor runs.
    """
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    reg_path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    wid = worker_id or default_worker_id()
    initialize_database(path)

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
            mark_failure(conn, job.id, error=str(exc), blocked=True)
            return WorkerResult(
                status="blocked",
                job_human_id=job.human_id,
                message=str(exc),
                exit_code=1,
            )

        job = mark_running(conn, job.id)

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
                info = ensure_worktree(
                    Path(job.repository_root),
                    name=name,
                    path=worktree_path,
                )
                worktree_name = info.name
                worktree_path = info.path
                base_sha = info.base_sha
                workspace = info.path
                job = update_job_worktree(
                    conn,
                    job.id,
                    worktree_name=info.name,
                    worktree_path=str(info.path),
                    base_git_sha=info.base_sha,
                )
            else:
                base_sha = base_sha or current_head_sha(Path(job.repository_root))
        except (WorktreeError, ProjectOSError) as exc:
            mark_failure(conn, job.id, error=str(exc), blocked=True)
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
                mark_failure(conn, job.id, error=str(exc), blocked=True)
                return WorkerResult(
                    status="blocked",
                    job_human_id=job.human_id,
                    message=str(exc),
                    exit_code=1,
                )

        prompt = build_role_prompt(
            job,
            workspace_path=str(workspace),
            base_git_sha=base_sha,
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

    # --- Phase 2: Cursor invocation (NO SQLite connection held) ---
    cursor_result: CursorRunResult | None = None
    run_error: str | None = None
    candidate_sha: str | None = None
    dirty: bool | None = None
    try:
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
        evidence_root = (
            worktree_path
            if worktree_path is not None and worktree_path.exists()
            else Path(job_repo)
        )
        if job_queue == "DELIVERY" and worktree_path is not None:
            # Governed candidate commit: do not leave dirty trees as success.
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
        insert_agent_run(
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

        if run_error or cursor_result is None:
            err = run_error or "cursor adapter returned no result"
            final = mark_failure(
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
            final = mark_failure(
                conn,
                job_id,
                error=detail,
                output_ref=cursor_result.output_ref,
            )
            if final.queue in ASSURANCE_QUEUES and final.source_candidate_sha:
                try:
                    record_assurance_result(
                        conn,
                        final,
                        passed=False,
                        evidence_ref=cursor_result.output_ref,
                        create_defect_fn=create_defect,
                    )
                except Exception:
                    pass
            return WorkerResult(
                status=final.status.lower(),
                job_human_id=job_human,
                message=detail,
                exit_code=1,
            )

        if job_queue in ASSURANCE_QUEUES and source_delivery_job_id:
            delivery = get_job(conn, source_delivery_job_id)
            if delivery.outcome in {"INVALIDATED", "SUPERSEDED", "NO_CHANGE"}:
                msg = (
                    f"Source delivery {delivery.human_id} outcome="
                    f"{delivery.outcome}; assurance cannot approve"
                )
                final = mark_failure(conn, job_id, error=msg, blocked=True)
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
                final = mark_failure(conn, job_id, error=msg, blocked=True)
                try:
                    record_assurance_result(
                        conn,
                        get_job(conn, job_id),
                        passed=False,
                        evidence_ref=cursor_result.output_ref,
                    )
                except Exception:
                    pass
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
                final = mark_failure(conn, job_id, error=err, blocked=True)
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
                final = mark_failure(conn, job_id, error=err, blocked=True)
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
                final = mark_failure(
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

        final = mark_succeeded(
            conn,
            job_id,
            output_ref=cursor_result.output_ref,
            candidate_git_sha=candidate_sha,
            outcome=outcome,
        )
        assert active_lease_for_job(conn, job_id) is None

        if final.queue == "DELIVERY" and is_valid_qa_candidate(final):
            maybe_handoff_after_delivery(conn, final)
        elif final.queue in ASSURANCE_QUEUES and final.source_candidate_sha:
            record_assurance_result(
                conn,
                final,
                passed=True,
                evidence_ref=cursor_result.output_ref,
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
