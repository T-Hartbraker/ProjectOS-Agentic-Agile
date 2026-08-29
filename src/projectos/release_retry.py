"""Governed reconciliation of a stale RELEASE attempt.

Preserves historical RELEASE rows. When the named job already executed against
a non-authoritative candidate, creates a successor bound to the SUCCEEDED
INTEGRATION candidate. Does not dispatch a worker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from projectos.db import connection
from projectos.errors import OrchestrationError
from projectos.migrate import initialize_database
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.release_provenance import (
    bind_release_provenance,
    integration_allows_release,
    integration_qa_gates_satisfied,
    release_prerequisites_met,
)
from projectos.store import (
    OrchestrationJob,
    active_lease_for_job,
    add_job_dependency,
    append_run_event,
    create_job,
    get_job,
    get_job_by_human_id,
    list_job_dependencies,
    promote_queued_to_ready,
    set_job_outcome,
)

REQUIRED_PROJECT = "PRJ-003"
REQUIRED_ITERATION = "ITER-002"
AUTHORITATIVE_INTEGRATION_SHA = "5811c17730849fe0282db06690f9d9d7cd5315a1"

_RETRY_MARKER = "__RETRY-"
_BUSY_STATUSES = frozenset({"RUNNING", "LEASED"})
_RETRYABLE_STATUSES = frozenset({"BLOCKED", "FAILED"})
_SUPERSEDED = "SUPERSEDED"
_INVALID_OUTCOMES = frozenset({"INVALIDATED", "SUPERSEDED", "NO_CHANGE"})


@dataclass(frozen=True)
class ReleaseReconcileResult:
    job_human_id: str
    status: str
    outcome: str | None
    attempt: int
    candidate_git_sha: str | None
    successor_job_human_id: str | None = None
    successor_status: str | None = None
    source_candidate_sha: str | None = None
    integration_job_human_id: str | None = None
    already_reconciled: bool = False
    message: str = ""
    exit_code: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


ReleaseRetryResult = ReleaseReconcileResult


def _fail(
    job: OrchestrationJob | None,
    message: str,
    *,
    integration_job_human_id: str | None = None,
) -> ReleaseReconcileResult:
    return ReleaseReconcileResult(
        job_human_id=job.human_id if job else "",
        status=job.status if job else "UNKNOWN",
        outcome=job.outcome if job else None,
        attempt=job.attempt if job else 0,
        candidate_git_sha=job.candidate_git_sha if job else None,
        integration_job_human_id=integration_job_human_id,
        message=message,
        exit_code=1,
    )


def _identity_snapshot(job: OrchestrationJob) -> dict:
    if job.identity_snapshot_json:
        try:
            data = json.loads(job.identity_snapshot_json)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {
        "project_human_id": job.project_human_id,
        "repository_root": job.repository_root,
        "iteration_human_id": job.iteration_human_id,
    }


def _assignment(job: OrchestrationJob) -> dict | None:
    if not job.assignment_json:
        return None
    try:
        data = json.loads(job.assignment_json)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _retry_base(human_id: str) -> str:
    if _RETRY_MARKER in human_id:
        return human_id.split(_RETRY_MARKER, 1)[0]
    return human_id


def _next_retry_human_id(conn, base_human_id: str) -> str:
    n = 1
    while get_job_by_human_id(conn, f"{base_human_id}{_RETRY_MARKER}{n}") is not None:
        n += 1
    return f"{base_human_id}{_RETRY_MARKER}{n}"


def _walk_successor(conn, job: OrchestrationJob) -> OrchestrationJob:
    seen: set[int] = set()
    current = job
    while current.superseded_by_job_id:
        if current.id in seen:
            break
        seen.add(current.id)
        current = get_job(conn, current.superseded_by_job_id)
    return current


def _copy_sponsor(conn, source: OrchestrationJob, target_id: int) -> None:
    if not source.sponsor_authority:
        return
    conn.execute(
        """
        UPDATE orchestration_jobs
        SET sponsor_authority = ?
        WHERE id = ?
        """,
        (source.sponsor_authority, target_id),
    )


def _find_integrations(conn, release: OrchestrationJob) -> list[OrchestrationJob]:
    found: dict[int, OrchestrationJob] = {}
    for dep_id in list_job_dependencies(conn, release.id):
        dep = get_job(conn, dep_id)
        if dep.queue == "INTEGRATION":
            found[dep.id] = dep
    rows = conn.execute(
        """
        SELECT id FROM orchestration_jobs
        WHERE queue = 'INTEGRATION'
          AND project_human_id = ?
          AND IFNULL(iteration_human_id, '') = IFNULL(?, '')
        ORDER BY id ASC
        """,
        (release.project_human_id, release.iteration_human_id),
    ).fetchall()
    for row in rows:
        job = get_job(conn, int(row[0]))
        found[job.id] = job
    return list(found.values())


def _resolve_integration(
    conn, release: OrchestrationJob
) -> tuple[OrchestrationJob, str] | ReleaseReconcileResult:
    candidates = _find_integrations(conn, release)
    if not candidates:
        return _fail(
            release,
            "reconcile refused: integration is not successful "
            "(no INTEGRATION job found for this RELEASE identity)",
        )

    succeeded = [c for c in candidates if c.status == "SUCCEEDED"]
    if not succeeded:
        return _fail(release, "reconcile refused: integration is not successful")

    valid_outcome = [c for c in succeeded if c.outcome not in _INVALID_OUTCOMES]
    if not valid_outcome:
        return _fail(
            release,
            "reconcile refused: integration is not successful "
            f"(outcome={succeeded[0].outcome})",
        )

    with_sha = [c for c in valid_outcome if c.candidate_git_sha]
    if not with_sha:
        return _fail(
            release,
            "reconcile refused: integration candidate is missing",
        )

    identity_ok = [
        c
        for c in with_sha
        if c.project_human_id == release.project_human_id
        and c.iteration_human_id == release.iteration_human_id
    ]
    if not identity_ok:
        return _fail(
            release,
            "reconcile refused: project/iteration identity mismatch "
            "between RELEASE and INTEGRATION",
        )

    by_sha: dict[str, list[OrchestrationJob]] = {}
    for integ in identity_ok:
        by_sha.setdefault(integ.candidate_git_sha or "", []).append(integ)
    if len(by_sha) > 1:
        return _fail(
            release,
            "reconcile refused: ambiguous provenance "
            f"(multiple INTEGRATION candidates {sorted(by_sha)})",
        )
    sha, integs = next(iter(by_sha.items()))
    integ = integs[0]

    if sha != AUTHORITATIVE_INTEGRATION_SHA:
        return _fail(
            release,
            "reconcile refused: integration candidate is not the authoritative SHA "
            f"(got {sha}, expected {AUTHORITATIVE_INTEGRATION_SHA})",
            integration_job_human_id=integ.human_id,
        )

    if not integration_qa_gates_satisfied(conn, integ):
        return _fail(
            release,
            "reconcile refused: required upstream QA gates are incomplete",
            integration_job_human_id=integ.human_id,
        )
    if not integration_allows_release(conn, integ, release):
        return _fail(
            release,
            "reconcile refused: INTEGRATION/QA/identity gates are not satisfied",
            integration_job_human_id=integ.human_id,
        )
    return integ, sha


def _bind_successor(
    conn, job: OrchestrationJob, integ: OrchestrationJob
) -> OrchestrationJob | ReleaseReconcileResult:
    if integ.id not in list_job_dependencies(conn, job.id):
        add_job_dependency(conn, job.id, integ.id)
    job = get_job(conn, job.id)
    if not release_prerequisites_met(conn, job):
        return _fail(
            job,
            "reconcile refused: INTEGRATION/QA/identity gates are not satisfied",
            integration_job_human_id=integ.human_id,
        )
    try:
        job = bind_release_provenance(conn, job)
    except OrchestrationError as exc:
        return _fail(
            job,
            f"reconcile refused: {exc}",
            integration_job_human_id=integ.human_id,
        )
    if job.status == "QUEUED":
        job = promote_queued_to_ready(
            conn,
            job.id,
            reason=(
                "RELEASE promoted to READY after governed reconciliation bound "
                "the integrated candidate"
            ),
        )
    return get_job(conn, job.id)


def reconcile_stale_release(
    *,
    job_human_id: str,
    db_path: Path | None = None,
    registry_path: Path | None = None,
) -> ReleaseReconcileResult:
    """Reconcile a stale RELEASE job onto the authoritative integration candidate.

    Historical executed attempts keep their status, SHAs, attempt counter,
    and events. A successor is created and bound; the worker is not dispatched.
    """
    db = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    _ = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    initialize_database(db)

    with connection(db) as conn:
        job = get_job_by_human_id(conn, job_human_id)
        if job is None:
            return _fail(None, f"reconcile refused: job {job_human_id!r} not found")
        if job.queue != "RELEASE" or job.agent_role != "RELEASE":
            return _fail(
                job,
                "reconcile refused: job queue/role must be RELEASE "
                f"(got queue={job.queue} role={job.agent_role})",
            )
        if job.project_human_id != REQUIRED_PROJECT:
            return _fail(
                job,
                "reconcile refused: project/iteration identity mismatch "
                f"(project={job.project_human_id!r}, expected {REQUIRED_PROJECT})",
            )
        if job.iteration_human_id != REQUIRED_ITERATION:
            return _fail(
                job,
                "reconcile refused: project/iteration identity mismatch "
                f"(iteration={job.iteration_human_id!r}, expected {REQUIRED_ITERATION})",
            )
        if job.status in _BUSY_STATUSES or active_lease_for_job(conn, job.id):
            return _fail(
                job,
                "reconcile refused: job is RUNNING/LEASED or still has an active lease",
            )
        if job.status == "RETRY_WAIT":
            return _fail(
                job,
                "reconcile refused: job is RETRY_WAIT; reclaim it before release reconcile",
            )

        resolved = _resolve_integration(conn, job)
        if isinstance(resolved, ReleaseReconcileResult):
            return resolved
        integ, sha = resolved

        recorded = job.candidate_git_sha or job.source_candidate_sha
        tip = _walk_successor(conn, job)
        retryable = (
            job.status in _RETRYABLE_STATUSES or tip.status in _RETRYABLE_STATUSES
        )
        if (recorded == sha or not recorded) and not retryable:
            return _fail(
                job,
                "reconcile refused: no stale provenance defect exists "
                "(release candidate does not differ from the integrated candidate)",
                integration_job_human_id=integ.human_id,
            )

        if (
            tip.id != job.id
            and (tip.source_candidate_sha == sha or tip.base_git_sha == sha)
            and tip.status not in _RETRYABLE_STATUSES
        ):
            named = get_job(conn, job.id)
            return ReleaseReconcileResult(
                job_human_id=named.human_id,
                status=named.status,
                outcome=named.outcome,
                attempt=named.attempt,
                candidate_git_sha=named.candidate_git_sha,
                successor_job_human_id=tip.human_id,
                successor_status=tip.status,
                source_candidate_sha=sha,
                integration_job_human_id=integ.human_id,
                already_reconciled=True,
                message=(
                    f"{tip.human_id} already reconciled to integrated "
                    f"candidate {sha} (not dispatched)"
                ),
                exit_code=0,
            )

        original_status = job.status
        original_sha = job.candidate_git_sha
        original_attempt = job.attempt
        original_completed = job.completed_at

        retry_hid = _next_retry_human_id(conn, _retry_base(job.human_id))
        successor = create_job(
            conn,
            human_id=retry_hid,
            project_human_id=job.project_human_id,
            repository_root=job.repository_root,
            agent_role="RELEASE",
            queue="RELEASE",
            status="QUEUED",
            priority=job.priority,
            max_attempts=job.max_attempts,
            iteration_human_id=job.iteration_human_id,
            work_item_type=job.work_item_type,
            work_item_human_id=job.work_item_human_id,
            worktree_name=f"{job.project_human_id}__{retry_hid}",
            base_git_sha=sha,
            requires_worktree=True,
            identity_snapshot=_identity_snapshot(job),
            assignment=_assignment(job),
            allows_no_change=job.allows_no_change,
        )
        _copy_sponsor(conn, job, successor.id)

        existing_deps = set(list_job_dependencies(conn, successor.id))
        for dep_id in list_job_dependencies(conn, job.id):
            if dep_id != successor.id and dep_id not in existing_deps:
                add_job_dependency(conn, successor.id, dep_id)
                existing_deps.add(dep_id)

        bound = _bind_successor(conn, successor, integ)
        if isinstance(bound, ReleaseReconcileResult):
            return bound
        successor = bound

        set_job_outcome(
            conn,
            job.id,
            outcome=_SUPERSEDED,
            superseded_by_job_id=successor.id,
        )
        if tip.id not in {job.id, successor.id}:
            set_job_outcome(
                conn,
                tip.id,
                outcome=_SUPERSEDED,
                superseded_by_job_id=successor.id,
            )
        append_run_event(
            conn,
            job.id,
            "release.stale_attempt_superseded",
            status=original_status,
            message=(
                "Historical RELEASE attempt preserved; successor bound to "
                f"integrated candidate {sha}"
            ),
            payload={
                "successor_job": successor.human_id,
                "stale_candidate_git_sha": original_sha,
                "source_candidate_sha": sha,
                "source_integration_job_id": integ.id,
                "attempt_preserved": original_attempt,
            },
        )
        append_run_event(
            conn,
            successor.id,
            "release.retry_created",
            status=successor.status,
            message=(
                f"Successor of {job.human_id} bound to integrated candidate {sha}"
            ),
            payload={
                "supersedes": job.human_id,
                "source_integration_job_id": integ.id,
                "source_candidate_sha": sha,
                "dispatched": False,
            },
        )

        named = get_job(conn, job.id)
        successor = get_job(conn, successor.id)
        if named.status != original_status or named.candidate_git_sha != original_sha:
            raise OrchestrationError(
                "reconcile aborted: historical RELEASE row was mutated"
            )
        if named.attempt != original_attempt:
            raise OrchestrationError(
                "reconcile aborted: historical attempt counter must be preserved"
            )
        if original_completed and named.completed_at != original_completed:
            raise OrchestrationError(
                "reconcile aborted: historical completed_at must be preserved"
            )
        if successor.started_at is not None:
            raise OrchestrationError(
                "reconcile aborted: successor must not be executed automatically"
            )

        return ReleaseReconcileResult(
            job_human_id=named.human_id,
            status=named.status,
            outcome=named.outcome,
            attempt=named.attempt,
            candidate_git_sha=named.candidate_git_sha,
            successor_job_human_id=successor.human_id,
            successor_status=successor.status,
            source_candidate_sha=successor.source_candidate_sha,
            integration_job_human_id=integ.human_id,
            already_reconciled=False,
            message=(
                f"Created successor {successor.human_id} bound to integrated "
                f"candidate {sha}; historical {named.human_id} preserved as "
                f"{named.outcome} (not dispatched)"
            ),
            exit_code=0,
        )


def retry_stale_release(
    *,
    job_human_id: str,
    db_path: Path | None = None,
    registry_path: Path | None = None,
) -> ReleaseReconcileResult:
    """Alias for reconcile_stale_release."""
    return reconcile_stale_release(
        job_human_id=job_human_id,
        db_path=db_path,
        registry_path=registry_path,
    )
