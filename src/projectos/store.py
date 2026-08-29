"""Orchestration job and lease persistence."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from projectos.errors import (
    ConflictError,
    CrossProjectWriteError,
    LeaseError,
    OrchestrationError,
)
from projectos.migrate import initialize_database

JOB_STATUSES = frozenset(
    {
        "QUEUED",
        "READY",
        "LEASED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "BLOCKED",
        "RETRY_WAIT",
        "CANCELLED",
    }
)
ACTIVE_WORKTREE_STATUSES = frozenset({"LEASED", "RUNNING"})
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


@dataclass(frozen=True)
class OrchestrationJob:
    id: int
    human_id: str
    project_human_id: str
    repository_root: str
    iteration_human_id: str | None
    work_item_type: str | None
    work_item_human_id: str | None
    agent_role: str
    queue: str
    status: str
    priority: int
    attempt: int
    max_attempts: int
    worktree_name: str | None
    worktree_path: str | None
    base_git_sha: str | None
    candidate_git_sha: str | None
    requires_worktree: bool
    identity_snapshot_json: str | None
    output_ref: str | None
    last_error: str | None
    created_at: str
    ready_at: str | None
    started_at: str | None
    completed_at: str | None
    updated_at: str
    source_delivery_job_id: int | None = None
    source_candidate_sha: str | None = None
    sponsor_authority: str | None = None
    outcome: str | None = None
    superseded_by_job_id: int | None = None
    assignment_json: str | None = None
    allows_no_change: bool = False

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> OrchestrationJob:
        keys = set(row.keys()) if hasattr(row, "keys") else set(row)

        def get(name: str, default: Any = None) -> Any:
            return row[name] if name in keys else default

        return cls(
            id=int(get("id")),
            human_id=str(get("human_id")),
            project_human_id=str(get("project_human_id")),
            repository_root=str(get("repository_root")),
            iteration_human_id=get("iteration_human_id"),
            work_item_type=get("work_item_type"),
            work_item_human_id=get("work_item_human_id"),
            agent_role=str(get("agent_role")),
            queue=str(get("queue")),
            status=str(get("status")),
            priority=int(get("priority")),
            attempt=int(get("attempt")),
            max_attempts=int(get("max_attempts")),
            worktree_name=get("worktree_name"),
            worktree_path=get("worktree_path"),
            base_git_sha=get("base_git_sha"),
            candidate_git_sha=get("candidate_git_sha"),
            requires_worktree=bool(get("requires_worktree")),
            identity_snapshot_json=get("identity_snapshot_json"),
            output_ref=get("output_ref"),
            last_error=get("last_error"),
            created_at=str(get("created_at")),
            ready_at=get("ready_at"),
            started_at=get("started_at"),
            completed_at=get("completed_at"),
            updated_at=str(get("updated_at")),
            source_delivery_job_id=(
                int(get("source_delivery_job_id"))
                if get("source_delivery_job_id") is not None
                else None
            ),
            source_candidate_sha=get("source_candidate_sha"),
            sponsor_authority=get("sponsor_authority"),
            outcome=get("outcome"),
            superseded_by_job_id=(
                int(get("superseded_by_job_id"))
                if get("superseded_by_job_id") is not None
                else None
            ),
            assignment_json=get("assignment_json"),
            allows_no_change=bool(get("allows_no_change") or 0),
        )


@dataclass(frozen=True)
class WorkerLease:
    id: int
    job_id: int
    worker_id: str
    leased_at: str
    expires_at: str
    released_at: str | None
    release_reason: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> WorkerLease:
        return cls(
            id=int(row["id"]),
            job_id=int(row["job_id"]),
            worker_id=str(row["worker_id"]),
            leased_at=str(row["leased_at"]),
            expires_at=str(row["expires_at"]),
            released_at=row["released_at"],
            release_reason=row["release_reason"],
        )


def ensure_db(db_path: Path | str) -> Path:
    path = Path(db_path)
    initialize_database(path)
    return path


def _row_to_job(conn: sqlite3.Connection, job_id: int) -> OrchestrationJob:
    row = conn.execute(
        "SELECT * FROM orchestration_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise OrchestrationError(f"Job id {job_id} not found")
    return OrchestrationJob.from_row(row)


def get_job_by_human_id(
    conn: sqlite3.Connection, human_id: str
) -> OrchestrationJob | None:
    row = conn.execute(
        "SELECT * FROM orchestration_jobs WHERE human_id = ?",
        (human_id,),
    ).fetchone()
    return OrchestrationJob.from_row(row) if row else None


@dataclass(frozen=True)
class RunEventRecord:
    id: int
    job_id: int
    job_human_id: str
    event_type: str
    status: str | None
    message: str | None
    payload_json: str | None
    created_at: str


def list_run_events_for_project(
    conn: sqlite3.Connection,
    project_human_id: str,
    *,
    limit: int = 50,
) -> list[RunEventRecord]:
    cap = max(1, min(int(limit), 200))
    rows = conn.execute(
        """
        SELECT e.id, e.job_id, j.human_id AS job_human_id, e.event_type, e.status,
               e.message, e.payload_json, e.created_at
        FROM run_events e
        INNER JOIN orchestration_jobs j ON j.id = e.job_id
        WHERE j.project_human_id = ?
        ORDER BY e.id DESC
        LIMIT ?
        """,
        (project_human_id, cap),
    ).fetchall()
    return [
        RunEventRecord(
            id=int(r["id"]),
            job_id=int(r["job_id"]),
            job_human_id=str(r["job_human_id"]),
            event_type=str(r["event_type"]),
            status=r["status"],
            message=r["message"],
            payload_json=r["payload_json"],
            created_at=str(r["created_at"]),
        )
        for r in rows
    ]


def list_dependency_graph_for_project(
    conn: sqlite3.Connection,
    project_human_id: str,
) -> tuple[list[OrchestrationJob], list[tuple[str, str]]]:
    """Return project jobs and (job_human_id, depends_on_human_id) edges."""
    jobs = list_jobs_for_project(conn, project_human_id)
    by_id = {job.id: job for job in jobs}
    edges: list[tuple[str, str]] = []
    for job in jobs:
        for dep_id in list_job_dependencies(conn, job.id):
            dep = by_id.get(dep_id)
            if dep is not None:
                edges.append((job.human_id, dep.human_id))
    return jobs, edges


def get_job(conn: sqlite3.Connection, job_id: int) -> OrchestrationJob:
    return _row_to_job(conn, job_id)


def _root_key(value: str | Path) -> str:
    try:
        return str(Path(value).resolve()).casefold()
    except (OSError, RuntimeError):
        return str(value).replace("\\", "/").rstrip("/").casefold()


def job_project_human_id(conn: sqlite3.Connection, job_id: int) -> str:
    row = conn.execute(
        "SELECT project_human_id FROM orchestration_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise OrchestrationError(f"Job id {job_id} not found")
    return str(row["project_human_id"])


def require_same_project(
    conn: sqlite3.Connection,
    *job_ids: int,
    action: str,
) -> str:
    """Return the shared project_human_id or raise CrossProjectWriteError."""
    if not job_ids:
        raise OrchestrationError(f"{action}: no jobs provided")
    seen: list[tuple[int, str]] = []
    for job_id in job_ids:
        seen.append((job_id, job_project_human_id(conn, job_id)))
    projects = {pid for _, pid in seen}
    if len(projects) != 1:
        detail = ", ".join(f"{jid}->{pid}" for jid, pid in seen)
        raise CrossProjectWriteError(
            f"{action} refused: jobs span projects ({detail})"
        )
    return next(iter(projects))


def _assert_create_job_isolation(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    repository_root: str | Path,
    identity_snapshot: dict[str, Any] | None,
) -> None:
    root = str(repository_root)
    if identity_snapshot:
        snap_pid = identity_snapshot.get("project_human_id")
        if snap_pid and str(snap_pid) != project_human_id:
            raise CrossProjectWriteError(
                "create_job refused: identity_snapshot.project_human_id "
                f"{snap_pid!r} != {project_human_id!r}"
            )
        snap_root = identity_snapshot.get("repository_root")
        if snap_root and _root_key(snap_root) != _root_key(root):
            raise CrossProjectWriteError(
                "create_job refused: identity_snapshot.repository_root "
                f"{snap_root!r} != {root!r}"
            )
    row = conn.execute(
        """
        SELECT repository_root FROM orchestration_jobs
        WHERE project_human_id = ?
        LIMIT 1
        """,
        (project_human_id,),
    ).fetchone()
    if row is not None and _root_key(row["repository_root"]) != _root_key(root):
        raise CrossProjectWriteError(
            "create_job refused: project "
            f"{project_human_id!r} is bound to {row['repository_root']}, "
            f"not {root}"
        )
    others = conn.execute(
        """
        SELECT project_human_id, repository_root FROM orchestration_jobs
        WHERE project_human_id != ?
        """,
        (project_human_id,),
    ).fetchall()
    for other in others:
        if _root_key(other["repository_root"]) == _root_key(root):
            raise CrossProjectWriteError(
                "create_job refused: repository_root "
                f"{root} already belongs to {other['project_human_id']}"
            )


def create_job(
    conn: sqlite3.Connection,
    *,
    human_id: str,
    project_human_id: str,
    repository_root: str | Path,
    agent_role: str,
    queue: str,
    status: str = "READY",
    priority: int = 100,
    attempt: int = 0,
    max_attempts: int = 3,
    iteration_human_id: str | None = None,
    work_item_type: str | None = None,
    work_item_human_id: str | None = None,
    worktree_name: str | None = None,
    worktree_path: str | None = None,
    base_git_sha: str | None = None,
    requires_worktree: bool = False,
    identity_snapshot: dict[str, Any] | None = None,
    assignment: dict[str, Any] | None = None,
    allows_no_change: bool = False,
) -> OrchestrationJob:
    if status not in JOB_STATUSES:
        raise OrchestrationError(f"Invalid job status {status!r}")
    _assert_create_job_isolation(
        conn,
        project_human_id=project_human_id,
        repository_root=repository_root,
        identity_snapshot=identity_snapshot,
    )
    now = utc_now_iso()
    ready_at = now if status == "READY" else None
    identity_json = (
        json.dumps(identity_snapshot, sort_keys=True) if identity_snapshot else None
    )
    assignment_json = (
        json.dumps(assignment, sort_keys=True) if assignment is not None else None
    )
    cur = conn.execute(
        """
        INSERT INTO orchestration_jobs (
            human_id, project_human_id, repository_root, iteration_human_id,
            work_item_type, work_item_human_id, agent_role, queue, status,
            priority, attempt, max_attempts, worktree_name, worktree_path,
            base_git_sha, requires_worktree, identity_snapshot_json,
            created_at, ready_at, updated_at, assignment_json, allows_no_change
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            human_id,
            project_human_id,
            str(repository_root),
            iteration_human_id,
            work_item_type,
            work_item_human_id,
            agent_role,
            queue,
            status,
            priority,
            attempt,
            max_attempts,
            worktree_name,
            worktree_path,
            base_git_sha,
            1 if requires_worktree else 0,
            identity_json,
            now,
            ready_at,
            now,
            assignment_json,
            1 if allows_no_change else 0,
        ),
    )
    return _row_to_job(conn, int(cur.lastrowid))


def append_run_event(
    conn: sqlite3.Connection,
    job_id: int,
    event_type: str,
    *,
    status: str | None = None,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    job_pid = job_project_human_id(conn, job_id)
    if payload and payload.get("project_human_id"):
        if str(payload["project_human_id"]) != job_pid:
            raise CrossProjectWriteError(
                "append_run_event refused: payload.project_human_id "
                f"{payload['project_human_id']!r} != job {job_pid!r}"
            )
    conn.execute(
        """
        INSERT INTO run_events (job_id, event_type, status, message, payload_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            job_id,
            event_type,
            status,
            message,
            json.dumps(payload, sort_keys=True) if payload is not None else None,
        ),
    )


def select_ready_job(
    conn: sqlite3.Connection,
    *,
    queue: str | None = None,
    role: str | None = None,
    job_human_id: str | None = None,
) -> OrchestrationJob | None:
    clauses = ["status = 'READY'"]
    params: list[Any] = []
    if job_human_id:
        clauses.append("human_id = ?")
        params.append(job_human_id)
    if queue:
        clauses.append("queue = ?")
        params.append(queue)
    if role:
        clauses.append("agent_role = ?")
        params.append(role)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT * FROM orchestration_jobs
        WHERE {where}
        ORDER BY priority DESC, COALESCE(ready_at, created_at) ASC, id ASC
        """,
        params,
    ).fetchall()
    for row in rows:
        job = OrchestrationJob.from_row(row)
        if dependencies_satisfied(conn, job.id):
            return job
    return None


def recover_expired_leases(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    project_human_id: str | None = None,
) -> list[int]:
    """Release expired leases and apply retry/ready policy.

    Policy:
    - LEASED (never started): return to READY without burning an attempt.
    - RUNNING (interrupted): increment attempt; RETRY_WAIT or FAILED.
    Candidate SHA, worktree metadata, and repository_root are preserved.
    """
    moment = now or utc_now()
    now_iso = moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    rows = conn.execute(
        """
        SELECT l.id AS lease_id, l.job_id, l.expires_at, j.status,
               j.attempt, j.max_attempts, j.project_human_id
        FROM worker_leases l
        JOIN orchestration_jobs j ON j.id = l.job_id
        WHERE l.released_at IS NULL
          AND j.status IN ('LEASED', 'RUNNING')
        """
    ).fetchall()
    recovered: list[int] = []
    for row in rows:
        expires = parse_iso(str(row["expires_at"]))
        if expires is None:
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires > moment:
            continue
        if project_human_id and str(row["project_human_id"]) != project_human_id:
            continue
        job_id = int(row["job_id"])
        prior_status = str(row["status"])
        attempt = int(row["attempt"])
        max_attempts = int(row["max_attempts"])

        conn.execute(
            """
            UPDATE worker_leases
            SET released_at = ?, release_reason = 'expired'
            WHERE id = ? AND released_at IS NULL
            """,
            (now_iso, int(row["lease_id"])),
        )

        if prior_status == "LEASED":
            new_status = "READY"
            new_attempt = attempt
            error = "lease expired before start; recovered to READY"
        else:
            new_attempt = attempt + 1
            if new_attempt >= max_attempts:
                new_status = "FAILED"
                error = (
                    "lease expired while RUNNING; max attempts exhausted "
                    f"({new_attempt}/{max_attempts})"
                )
            else:
                new_status = "RETRY_WAIT"
                error = (
                    "lease expired while RUNNING; recovered to RETRY_WAIT "
                    f"(attempt {new_attempt}/{max_attempts})"
                )

        conn.execute(
            """
            UPDATE orchestration_jobs
            SET status = ?,
                attempt = ?,
                started_at = NULL,
                completed_at = CASE WHEN ? = 'FAILED' THEN ? ELSE completed_at END,
                updated_at = ?,
                last_error = ?,
                ready_at = CASE WHEN ? = 'READY' THEN COALESCE(ready_at, ?) ELSE ready_at END
            WHERE id = ?
            """,
            (
                new_status,
                new_attempt,
                new_status,
                now_iso,
                now_iso,
                error,
                new_status,
                now_iso,
                job_id,
            ),
        )
        append_run_event(
            conn,
            job_id,
            "lease.expired_recovered",
            status=new_status,
            message=error,
            payload={
                "prior_status": prior_status,
                "attempt": new_attempt,
                "max_attempts": max_attempts,
            },
        )
        recovered.append(job_id)
    return recovered


def reclaim_interrupted_running_job(
    conn: sqlite3.Connection,
    job_human_id: str,
    *,
    reason: str = "operator reclaim of interrupted RUNNING job",
) -> OrchestrationJob:
    """Governed reclaim: expire active lease then apply expired-lease policy.

    Does not rewrite work-item identity, base SHA, or repository binding.
    """
    job = get_job_by_human_id(conn, job_human_id)
    if job is None:
        raise OrchestrationError(f"Job {job_human_id!r} not found")
    if job.status not in {"RUNNING", "LEASED"}:
        raise OrchestrationError(
            f"Job {job_human_id} is {job.status}, expected RUNNING or LEASED"
        )
    past = (utc_now() - timedelta(seconds=5)).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    conn.execute(
        """
        UPDATE worker_leases
        SET expires_at = ?
        WHERE job_id = ? AND released_at IS NULL
        """,
        (past, job.id),
    )
    append_run_event(
        conn,
        job.id,
        "lease.reclaim_requested",
        status=job.status,
        message=reason,
        payload={"previous_status": job.status},
    )
    recovered = recover_expired_leases(conn)
    if job.id not in recovered:
        release_lease(conn, job.id, reason="reclaim_interrupted")
        return mark_failure(conn, job.id, error=reason, blocked=False)
    return get_job(conn, job.id)


def promote_retry_wait_to_ready(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    reason: str = "recovery promoted RETRY_WAIT to READY",
) -> OrchestrationJob:
    now = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE orchestration_jobs
        SET status = 'READY',
            ready_at = ?,
            updated_at = ?,
            last_error = NULL
        WHERE id = ? AND status = 'RETRY_WAIT'
        """,
        (now, now, job_id),
    )
    if cur.rowcount != 1:
        raise OrchestrationError(
            f"Cannot promote job {job_id} to READY: expected RETRY_WAIT"
        )
    append_run_event(
        conn,
        job_id,
        "job.promoted_ready",
        status="READY",
        message=reason,
    )
    return _row_to_job(conn, job_id)


def promote_queued_to_ready(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    reason: str = "prerequisites satisfied; QUEUED promoted to READY",
) -> OrchestrationJob:
    """QUEUED -> READY without overwriting an existing ready_at timestamp."""
    job = _row_to_job(conn, job_id)
    if job.status != "QUEUED":
        return job
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE orchestration_jobs
        SET status = 'READY',
            ready_at = COALESCE(ready_at, ?),
            updated_at = ?,
            last_error = NULL
        WHERE id = ? AND status = 'QUEUED'
        """,
        (now, now, job_id),
    )
    append_run_event(
        conn,
        job_id,
        "job.promoted_ready",
        status="READY",
        message=reason,
        payload={"previous_status": "QUEUED"},
    )
    return _row_to_job(conn, job_id)


def mark_blocked(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    error: str,
    release_reason: str = "blocked",
) -> OrchestrationJob:
    """Block a job without altering candidate provenance or repository binding."""
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE orchestration_jobs
        SET status = 'BLOCKED',
            last_error = ?,
            completed_at = ?,
            updated_at = ?,
            started_at = NULL
        WHERE id = ?
        """,
        (error, now, now, job_id),
    )
    append_run_event(
        conn,
        job_id,
        "job.blocked",
        status="BLOCKED",
        message=error,
    )
    release_lease(conn, job_id, reason=release_reason)
    return _row_to_job(conn, job_id)


def mark_ready_from_blocked(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    reason: str,
) -> OrchestrationJob:
    """Transition BLOCKED -> READY without rewriting identity or provenance.

    Historical BLOCKED run events and prior last_error text in those events
    remain; the active last_error column is cleared.
    """
    job = _row_to_job(conn, job_id)
    if job.status != "BLOCKED":
        raise OrchestrationError(
            f"Cannot revalidate job {job.human_id}: status is {job.status}, "
            "expected BLOCKED"
        )
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE orchestration_jobs
        SET status = 'READY',
            ready_at = ?,
            completed_at = NULL,
            started_at = NULL,
            last_error = NULL,
            updated_at = ?
        WHERE id = ? AND status = 'BLOCKED'
        """,
        (now, now, job_id),
    )
    append_run_event(
        conn,
        job_id,
        "job.revalidated_ready",
        status="READY",
        message=reason,
        payload={
            "previous_error": job.last_error,
            "work_item_type": job.work_item_type,
            "work_item_human_id": job.work_item_human_id,
            "base_git_sha": job.base_git_sha,
            "repository_root": job.repository_root,
            "project_human_id": job.project_human_id,
        },
    )
    return _row_to_job(conn, job_id)


def list_jobs_by_statuses(
    conn: sqlite3.Connection,
    statuses: frozenset[str] | set[str],
) -> list[OrchestrationJob]:
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"""
        SELECT * FROM orchestration_jobs
        WHERE status IN ({placeholders})
        ORDER BY id ASC
        """,
        tuple(sorted(statuses)),
    ).fetchall()
    return [OrchestrationJob.from_row(r) for r in rows]


def list_jobs_for_project(
    conn: sqlite3.Connection,
    project_human_id: str,
    *,
    statuses: frozenset[str] | set[str] | None = None,
) -> list[OrchestrationJob]:
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"""
            SELECT * FROM orchestration_jobs
            WHERE project_human_id = ?
              AND status IN ({placeholders})
            ORDER BY id ASC
            """,
            (project_human_id, *sorted(statuses)),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM orchestration_jobs
            WHERE project_human_id = ?
            ORDER BY id ASC
            """,
            (project_human_id,),
        ).fetchall()
    return [OrchestrationJob.from_row(r) for r in rows]


def acquire_lease(
    conn: sqlite3.Connection,
    job: OrchestrationJob,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> WorkerLease:
    """Atomically transition READY -> LEASED and create a time-limited lease."""
    if lease_seconds <= 0:
        raise LeaseError("lease_seconds must be positive")
    recover_expired_leases(conn, now=now)
    moment = now or utc_now()
    leased_at = moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    expires_at = (moment + timedelta(seconds=lease_seconds)).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")

    # Clear any previously released lease row for this job (UNIQUE job_id).
    conn.execute(
        "DELETE FROM worker_leases WHERE job_id = ? AND released_at IS NOT NULL",
        (job.id,),
    )

    cur = conn.execute(
        """
        UPDATE orchestration_jobs
        SET status = 'LEASED', updated_at = ?
        WHERE id = ? AND status = 'READY'
        """,
        (leased_at, job.id),
    )
    if cur.rowcount != 1:
        raise LeaseError(
            f"Failed to acquire lease for job {job.human_id}: not READY "
            f"(current status may have changed)"
        )

    try:
        lease_cur = conn.execute(
            """
            INSERT INTO worker_leases (job_id, worker_id, leased_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (job.id, worker_id, leased_at, expires_at),
        )
    except sqlite3.IntegrityError as exc:
        conn.execute(
            """
            UPDATE orchestration_jobs
            SET status = 'READY', updated_at = ?
            WHERE id = ?
            """,
            (leased_at, job.id),
        )
        raise LeaseError(
            f"Failed to acquire lease for job {job.human_id}: lease conflict"
        ) from exc

    append_run_event(
        conn,
        job.id,
        "lease.acquired",
        status="LEASED",
        message=f"Lease acquired by {worker_id}",
        payload={"expires_at": expires_at, "lease_seconds": lease_seconds},
    )
    row = conn.execute(
        "SELECT * FROM worker_leases WHERE id = ?",
        (int(lease_cur.lastrowid),),
    ).fetchone()
    assert row is not None
    return WorkerLease.from_row(row)


def mark_running(conn: sqlite3.Connection, job_id: int) -> OrchestrationJob:
    now = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE orchestration_jobs
        SET status = 'RUNNING', started_at = ?, updated_at = ?
        WHERE id = ? AND status = 'LEASED'
        """,
        (now, now, job_id),
    )
    if cur.rowcount != 1:
        raise OrchestrationError(
            f"Cannot mark job {job_id} RUNNING: expected LEASED"
        )
    append_run_event(conn, job_id, "job.running", status="RUNNING")
    return _row_to_job(conn, job_id)


def release_lease(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    reason: str,
) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE worker_leases
        SET released_at = ?, release_reason = ?
        WHERE job_id = ? AND released_at IS NULL
        """,
        (now, reason, job_id),
    )
    append_run_event(
        conn,
        job_id,
        "lease.released",
        message=reason,
    )


def update_job_worktree(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    worktree_name: str,
    worktree_path: str,
    base_git_sha: str | None,
) -> OrchestrationJob:
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE orchestration_jobs
        SET worktree_name = ?, worktree_path = ?,
            base_git_sha = COALESCE(?, base_git_sha),
            updated_at = ?
        WHERE id = ?
        """,
        (worktree_name, worktree_path, base_git_sha, now, job_id),
    )
    return _row_to_job(conn, job_id)


def find_active_worktree_holder(
    conn: sqlite3.Connection,
    worktree_name: str,
    *,
    exclude_job_id: int | None = None,
) -> OrchestrationJob | None:
    params: list[Any] = [worktree_name, *sorted(ACTIVE_WORKTREE_STATUSES)]
    placeholders = ",".join("?" for _ in ACTIVE_WORKTREE_STATUSES)
    sql = f"""
        SELECT * FROM orchestration_jobs
        WHERE worktree_name = ?
          AND status IN ({placeholders})
    """
    if exclude_job_id is not None:
        sql += " AND id != ?"
        params.append(exclude_job_id)
    sql += " ORDER BY id ASC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return OrchestrationJob.from_row(row) if row else None


def mark_succeeded(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    output_ref: str | None,
    candidate_git_sha: str | None,
    outcome: str | None = None,
) -> OrchestrationJob:
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE orchestration_jobs
        SET status = 'SUCCEEDED',
            output_ref = ?,
            candidate_git_sha = COALESCE(?, candidate_git_sha),
            outcome = COALESCE(?, outcome),
            completed_at = ?,
            updated_at = ?,
            last_error = NULL
        WHERE id = ?
        """,
        (output_ref, candidate_git_sha, outcome, now, now, job_id),
    )
    append_run_event(
        conn,
        job_id,
        "job.succeeded",
        status="SUCCEEDED",
        message="Worker task completed (not QA/release acceptance)",
        payload={"outcome": outcome} if outcome else None,
    )
    release_lease(conn, job_id, reason="succeeded")
    return _row_to_job(conn, job_id)


def mark_cancelled(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    reason: str,
) -> OrchestrationJob:
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE orchestration_jobs
        SET status = 'CANCELLED',
            last_error = ?,
            completed_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (reason, now, now, job_id),
    )
    append_run_event(
        conn,
        job_id,
        "job.cancelled",
        status="CANCELLED",
        message=reason,
    )
    release_lease(conn, job_id, reason="cancelled")
    return _row_to_job(conn, job_id)


def set_job_outcome(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    outcome: str,
    superseded_by_job_id: int | None = None,
) -> OrchestrationJob:
    if superseded_by_job_id is not None:
        require_same_project(
            conn, job_id, superseded_by_job_id, action="set_job_outcome"
        )
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE orchestration_jobs
        SET outcome = ?,
            superseded_by_job_id = COALESCE(?, superseded_by_job_id),
            updated_at = ?
        WHERE id = ?
        """,
        (outcome, superseded_by_job_id, now, job_id),
    )
    return _row_to_job(conn, job_id)


def set_job_assignment(
    conn: sqlite3.Connection,
    job_id: int,
    assignment: dict[str, Any],
) -> OrchestrationJob:
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE orchestration_jobs
        SET assignment_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(assignment, sort_keys=True), now, job_id),
    )
    return _row_to_job(conn, job_id)


def mark_failure(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    error: str,
    output_ref: str | None = None,
    blocked: bool = False,
) -> OrchestrationJob:
    """Persist failure, apply retry policy, release lease."""
    job = _row_to_job(conn, job_id)
    now = utc_now_iso()
    attempt = job.attempt + 1
    if blocked:
        status = "BLOCKED"
    elif attempt >= job.max_attempts:
        status = "FAILED"
    else:
        status = "RETRY_WAIT"

    conn.execute(
        """
        UPDATE orchestration_jobs
        SET status = ?,
            attempt = ?,
            last_error = ?,
            output_ref = COALESCE(?, output_ref),
            completed_at = CASE WHEN ? IN ('FAILED', 'BLOCKED') THEN ? ELSE completed_at END,
            updated_at = ?
        WHERE id = ?
        """,
        (status, attempt, error, output_ref, status, now, now, job_id),
    )
    append_run_event(
        conn,
        job_id,
        "job.failed" if status != "RETRY_WAIT" else "job.retry_wait",
        status=status,
        message=error,
        payload={"attempt": attempt, "max_attempts": job.max_attempts},
    )
    release_lease(conn, job_id, reason=f"failure:{status}")
    return _row_to_job(conn, job_id)


def insert_agent_run(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    worker_id: str,
    cursor_command: list[str],
    prompt_ref: str | None,
    output_ref: str | None,
    stdout_ref: str | None,
    stderr_ref: str | None,
    exit_code: int | None,
    started_at: str | None,
    ended_at: str | None,
    duration_ms: int | None,
    worktree_name: str | None,
    worktree_path: str | None,
    base_git_sha: str | None,
    candidate_git_sha: str | None,
    dirty: bool | None,
    usage: dict[str, Any] | None,
    error: str | None,
) -> int:
    job_project_human_id(conn, job_id)
    cur = conn.execute(
        """
        INSERT INTO agent_runs (
            job_id, worker_id, cursor_command_json, prompt_ref, output_ref,
            stdout_ref, stderr_ref, exit_code, started_at, ended_at, duration_ms,
            worktree_name, worktree_path, base_git_sha, candidate_git_sha,
            dirty, usage_json, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            worker_id,
            json.dumps(cursor_command),
            prompt_ref,
            output_ref,
            stdout_ref,
            stderr_ref,
            exit_code,
            started_at,
            ended_at,
            duration_ms,
            worktree_name,
            worktree_path,
            base_git_sha,
            candidate_git_sha,
            None if dirty is None else (1 if dirty else 0),
            json.dumps(usage, sort_keys=True) if usage is not None else None,
            error,
        ),
    )
    return int(cur.lastrowid)


def latest_agent_run(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM agent_runs
        WHERE job_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()


def active_lease_for_job(
    conn: sqlite3.Connection, job_id: int
) -> WorkerLease | None:
    row = conn.execute(
        """
        SELECT * FROM worker_leases
        WHERE job_id = ? AND released_at IS NULL
        """,
        (job_id,),
    ).fetchone()
    return WorkerLease.from_row(row) if row else None


def add_job_dependency(
    conn: sqlite3.Connection, job_id: int, depends_on_job_id: int
) -> None:
    if job_id == depends_on_job_id:
        raise OrchestrationError("Job cannot depend on itself")
    require_same_project(conn, job_id, depends_on_job_id, action="add_job_dependency")
    conn.execute(
        """
        INSERT INTO orchestration_job_dependencies (job_id, depends_on_job_id)
        VALUES (?, ?)
        """,
        (job_id, depends_on_job_id),
    )


def list_job_dependencies(conn: sqlite3.Connection, job_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT depends_on_job_id FROM orchestration_job_dependencies
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def list_dependent_job_ids(conn: sqlite3.Connection, job_id: int) -> list[int]:
    """Job ids that declare a depends_on edge to job_id."""
    rows = conn.execute(
        """
        SELECT job_id FROM orchestration_job_dependencies
        WHERE depends_on_job_id = ?
        """,
        (job_id,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def job_satisfies_dependency(dep: OrchestrationJob) -> bool:
    """SUCCEEDED deps with invalidated/superseded/no-change outcomes do not count."""
    if dep.status != "SUCCEEDED":
        return False
    if dep.outcome in {"INVALIDATED", "SUPERSEDED", "NO_CHANGE"}:
        return False
    return True


def dependencies_satisfied(conn: sqlite3.Connection, job_id: int) -> bool:
    deps = list_job_dependencies(conn, job_id)
    for dep_id in deps:
        dep = _row_to_job(conn, dep_id)
        if not job_satisfies_dependency(dep):
            return False
    return True


def list_eligible_ready_jobs(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    queue: str | None = None,
    project_human_id: str | None = None,
) -> list[OrchestrationJob]:
    """READY jobs whose dependencies are all successfully satisfied."""
    clauses = ["status = 'READY'"]
    params: list[Any] = []
    if queue:
        clauses.append("queue = ?")
        params.append(queue)
    if project_human_id:
        clauses.append("project_human_id = ?")
        params.append(project_human_id)
    clauses.append(
        """
        project_human_id NOT IN (
            SELECT project_human_id FROM project_orchestration_control
            WHERE paused = 1
        )
        """
    )
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT * FROM orchestration_jobs
        WHERE {where}
        ORDER BY priority DESC, COALESCE(ready_at, created_at) ASC, id ASC
        """,
        params,
    ).fetchall()
    eligible: list[OrchestrationJob] = []
    for row in rows:
        job = OrchestrationJob.from_row(row)
        if dependencies_satisfied(conn, job.id):
            eligible.append(job)
            if limit is not None and len(eligible) >= limit:
                break
    return eligible


def set_job_source_provenance(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    source_delivery_job_id: int | None,
    source_candidate_sha: str | None,
) -> OrchestrationJob:
    if source_delivery_job_id is not None:
        require_same_project(
            conn, job_id, source_delivery_job_id, action="set_job_source_provenance"
        )
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE orchestration_jobs
        SET source_delivery_job_id = ?,
            source_candidate_sha = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (source_delivery_job_id, source_candidate_sha, now, job_id),
    )
    return _row_to_job(conn, job_id)


def find_active_assignment(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    work_item_human_id: str,
    queue: str,
) -> OrchestrationJob | None:
    """Duplicate active assignment detector for planning."""
    active = (
        "QUEUED",
        "READY",
        "LEASED",
        "RUNNING",
        "RETRY_WAIT",
    )
    placeholders = ",".join("?" for _ in active)
    row = conn.execute(
        f"""
        SELECT * FROM orchestration_jobs
        WHERE project_human_id = ?
          AND work_item_human_id = ?
          AND queue = ?
          AND status IN ({placeholders})
        ORDER BY id DESC LIMIT 1
        """,
        (project_human_id, work_item_human_id, queue, *active),
    ).fetchone()
    return OrchestrationJob.from_row(row) if row else None

def insert_qa_evidence(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    repository_root: str | Path,
    delivery_job_id: int,
    assurance_job_id: int,
    candidate_git_sha: str,
    assurance_role: str,
    result: str = "pending",
) -> int:
    pid = require_same_project(
        conn, delivery_job_id, assurance_job_id, action="insert_qa_evidence"
    )
    if pid != project_human_id:
        raise CrossProjectWriteError(
            "insert_qa_evidence refused: jobs belong to "
            f"{pid!r}, not {project_human_id!r}"
        )
    delivery = get_job(conn, delivery_job_id)
    if _root_key(delivery.repository_root) != _root_key(repository_root):
        raise CrossProjectWriteError(
            "insert_qa_evidence refused: repository_root "
            f"{repository_root} != delivery {delivery.repository_root}"
        )
    cur = conn.execute(
        """
        INSERT INTO qa_evidence (
            project_human_id, repository_root, delivery_job_id,
            assurance_job_id, candidate_git_sha, assurance_role, result
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_human_id,
            str(repository_root),
            delivery_job_id,
            assurance_job_id,
            candidate_git_sha,
            assurance_role,
            result,
        ),
    )
    return int(cur.lastrowid)


def insert_candidate_invalidation(
    conn: sqlite3.Connection,
    *,
    delivery_job_id: int,
    invalidated_candidate_sha: str | None,
    reason: str,
    rework_job_id: int | None,
) -> int:
    ids = [delivery_job_id]
    if rework_job_id is not None:
        ids.append(rework_job_id)
    require_same_project(conn, *ids, action="insert_candidate_invalidation")
    cur = conn.execute(
        """
        INSERT INTO candidate_invalidations (
            delivery_job_id, invalidated_candidate_sha, reason, rework_job_id
        ) VALUES (?, ?, ?, ?)
        """,
        (
            delivery_job_id,
            invalidated_candidate_sha,
            reason,
            rework_job_id,
        ),
    )
    return int(cur.lastrowid)


def insert_integration_run(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    repository_root: str | Path,
    iteration_human_id: str | None,
    source_job_ids: list[int],
    source_shas: list[str],
    status: str = "integrating",
    updated_at: str | None = None,
) -> int:
    if source_job_ids:
        existing: list[tuple[int, str]] = []
        for job_id in source_job_ids:
            row = conn.execute(
                "SELECT project_human_id FROM orchestration_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is not None:
                existing.append((job_id, str(row["project_human_id"])))
        if existing:
            projects = {pid for _, pid in existing}
            if len(projects) != 1 or next(iter(projects)) != project_human_id:
                raise CrossProjectWriteError(
                    "insert_integration_run refused: source jobs "
                    f"{existing} do not belong to {project_human_id!r}"
                )
            first = get_job(conn, existing[0][0])
            if _root_key(first.repository_root) != _root_key(repository_root):
                raise CrossProjectWriteError(
                    "insert_integration_run refused: repository_root "
                    f"{repository_root} != job {first.repository_root}"
                )
    now = updated_at or utc_now_iso()
    cur = conn.execute(
        """
        INSERT INTO integration_runs (
            project_human_id, repository_root, iteration_human_id,
            source_job_ids_json, source_shas_json, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_human_id,
            str(repository_root),
            iteration_human_id,
            json.dumps(source_job_ids),
            json.dumps(source_shas),
            status,
            now,
        ),
    )
    return int(cur.lastrowid)


def list_expired_lease_job_ids(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    project_human_id: str | None = None,
) -> list[int]:
    """Job ids whose leases are expired. Read-only; does not mutate."""
    moment = now or utc_now()
    rows = conn.execute(
        """
        SELECT l.job_id, l.expires_at, j.project_human_id
        FROM worker_leases l
        JOIN orchestration_jobs j ON j.id = l.job_id
        WHERE l.released_at IS NULL
          AND j.status IN ('LEASED', 'RUNNING')
        """
    ).fetchall()
    found: list[int] = []
    for row in rows:
        expires = parse_iso(str(row["expires_at"]))
        if expires is None:
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires > moment:
            continue
        if project_human_id and str(row["project_human_id"]) != project_human_id:
            continue
        found.append(int(row["job_id"]))
    return found


def list_assurance_for_project(
    conn: sqlite3.Connection, project_human_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.result, e.assurance_role, e.candidate_git_sha, e.defect_human_id,
               e.evidence_ref, e.created_at, a.status AS assurance_job_status,
               d.human_id AS delivery_job_human_id,
               a.human_id AS assurance_job_human_id
        FROM qa_evidence e
        LEFT JOIN orchestration_jobs d ON d.id = e.delivery_job_id
        LEFT JOIN orchestration_jobs a ON a.id = e.assurance_job_id
        WHERE e.project_human_id = ?
        ORDER BY e.id DESC
        """,
        (project_human_id,),
    ).fetchall()
    return [
        {
            "result": str(row["result"]),
            "assurance_role": str(row["assurance_role"]),
            "candidate_git_sha": str(row["candidate_git_sha"]),
            "defect_human_id": row["defect_human_id"],
            "delivery_job_human_id": row["delivery_job_human_id"],
            "assurance_job_human_id": row["assurance_job_human_id"],
            "assurance_job_status": row["assurance_job_status"],
            "evidence_ref": public_artifact_ref(row["evidence_ref"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def list_invalidations_for_project(
    conn: sqlite3.Connection, project_human_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT i.invalidated_candidate_sha, i.reason, i.created_at,
               d.human_id AS delivery_job_human_id,
               r.human_id AS rework_job_human_id
        FROM candidate_invalidations i
        INNER JOIN orchestration_jobs d ON d.id = i.delivery_job_id
        LEFT JOIN orchestration_jobs r ON r.id = i.rework_job_id
        WHERE d.project_human_id = ?
        ORDER BY i.id DESC
        """,
        (project_human_id,),
    ).fetchall()
    return [
        {
            "delivery_job_human_id": str(row["delivery_job_human_id"]),
            "rework_job_human_id": row["rework_job_human_id"],
            "invalidated_candidate_sha": row["invalidated_candidate_sha"],
            "reason": str(row["reason"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def list_integrations_for_project(
    conn: sqlite3.Connection, project_human_id: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    cap = max(1, min(int(limit), 50))
    rows = conn.execute(
        """
        SELECT iteration_human_id, source_job_ids_json, source_shas_json,
               integrated_sha, status, conflict_paths_json, error,
               created_at, updated_at
        FROM integration_runs
        WHERE project_human_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (project_human_id, cap),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        raw_ids = json.loads(row["source_job_ids_json"] or "[]")
        source_hids: list[str] = []
        if isinstance(raw_ids, list):
            for jid in raw_ids:
                try:
                    source_hids.append(get_job(conn, int(jid)).human_id)
                except OrchestrationError:
                    continue
        shas = json.loads(row["source_shas_json"] or "[]")
        conflicts = json.loads(row["conflict_paths_json"] or "[]") if row["conflict_paths_json"] else []
        out.append(
            {
                "iteration_human_id": row["iteration_human_id"],
                "status": str(row["status"]),
                "integrated_sha": row["integrated_sha"],
                "source_job_human_ids": source_hids,
                "source_sha_count": len(shas) if isinstance(shas, list) else 0,
                "conflict_count": len(conflicts) if isinstance(conflicts, list) else 0,
                "error": row["error"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
        )
    return out


def public_artifact_ref(value: str | None) -> str | None:
    """Return a basename-only evidence/log reference. Never a filesystem browse path."""
    if not value:
        return None
    text = str(value).replace("\\", "/").strip()
    if not text:
        return None
    return text.rsplit("/", 1)[-1]


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
ALLOWED_ARTIFACT_KINDS = frozenset(
    {
        "qa_package",
        "readiness",
        "manifest",
        "checksums",
        "notes",
        "rollback",
        "bundle",
    }
)


def require_safe_id(value: str, *, label: str) -> str:
    """Accept catalog identifiers only. Reject path traversal and separators."""
    text = str(value or "").strip()
    if (
        not text
        or not _SAFE_ID_RE.fullmatch(text)
        or ".." in text
        or "/" in text
        or "\\" in text
    ):
        raise OrchestrationError(f"{label} is not a valid identifier")
    return text


def require_safe_filename(value: str) -> str:
    text = str(value or "").strip()
    if not text or "/" in text or "\\" in text or ".." in text:
        raise OrchestrationError("artifact filename is not a valid identifier")
    return require_safe_id(text, label="artifact filename")


def upsert_release_artifact(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    release_human_id: str,
    artifact_human_id: str,
    filename: str,
    content: bytes,
    kind: str,
    media_type: str = "application/octet-stream",
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    release_id = require_safe_id(release_human_id, label="release_human_id")
    artifact_id = require_safe_id(artifact_human_id, label="artifact_human_id")
    safe_name = require_safe_filename(filename)
    kind_key = str(kind or "").strip()
    if kind_key not in ALLOWED_ARTIFACT_KINDS:
        raise OrchestrationError(f"artifact kind {kind!r} is not allowlisted")
    payload = bytes(content)
    if len(payload) > _MAX_ARTIFACT_BYTES:
        raise OrchestrationError("artifact exceeds maximum catalog size")
    digest = hashlib.sha256(payload).hexdigest()
    conn.execute(
        """
        INSERT INTO release_artifacts (
            project_human_id, release_human_id, artifact_human_id, filename,
            sha256, byte_size, media_type, kind, content, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (project_human_id, release_human_id, artifact_human_id)
        DO UPDATE SET
            filename = excluded.filename,
            sha256 = excluded.sha256,
            byte_size = excluded.byte_size,
            media_type = excluded.media_type,
            kind = excluded.kind,
            content = excluded.content
        """,
        (
            project,
            release_id,
            artifact_id,
            safe_name,
            digest,
            len(payload),
            media_type,
            kind_key,
            payload,
            utc_now_iso(),
        ),
    )
    return {
        "project_human_id": project,
        "release_human_id": release_id,
        "artifact_human_id": artifact_id,
        "filename": safe_name,
        "sha256": digest,
        "byte_size": len(payload),
        "media_type": media_type,
        "kind": kind_key,
    }


def list_release_artifacts(
    conn: sqlite3.Connection, project_human_id: str, release_human_id: str
) -> list[dict[str, Any]]:
    project = require_safe_id(project_human_id, label="project_human_id")
    release_id = require_safe_id(release_human_id, label="release_human_id")
    rows = conn.execute(
        """
        SELECT project_human_id, release_human_id, artifact_human_id, filename,
               sha256, byte_size, media_type, kind, created_at
        FROM release_artifacts
        WHERE project_human_id = ? AND release_human_id = ?
        ORDER BY id ASC
        """,
        (project, release_id),
    ).fetchall()
    return [
        {
            "project_human_id": str(row["project_human_id"]),
            "release_human_id": str(row["release_human_id"]),
            "artifact_human_id": str(row["artifact_human_id"]),
            "filename": str(row["filename"]),
            "sha256": str(row["sha256"]),
            "byte_size": int(row["byte_size"]),
            "media_type": str(row["media_type"]),
            "kind": str(row["kind"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def get_release_artifact(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    release_human_id: str,
    artifact_human_id: str,
) -> dict[str, Any] | None:
    project = require_safe_id(project_human_id, label="project_human_id")
    release_id = require_safe_id(release_human_id, label="release_human_id")
    artifact_id = require_safe_id(artifact_human_id, label="artifact_human_id")
    row = conn.execute(
        """
        SELECT project_human_id, release_human_id, artifact_human_id, filename,
               sha256, byte_size, media_type, kind, content, created_at
        FROM release_artifacts
        WHERE project_human_id = ? AND release_human_id = ? AND artifact_human_id = ?
        """,
        (project, release_id, artifact_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "project_human_id": str(row["project_human_id"]),
        "release_human_id": str(row["release_human_id"]),
        "artifact_human_id": str(row["artifact_human_id"]),
        "filename": str(row["filename"]),
        "sha256": str(row["sha256"]),
        "byte_size": int(row["byte_size"]),
        "media_type": str(row["media_type"]),
        "kind": str(row["kind"]),
        "content": bytes(row["content"]),
        "created_at": str(row["created_at"]),
    }


def list_agent_runs_for_project(
    conn: sqlite3.Connection, project_human_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    cap = max(1, min(int(limit), 100))
    rows = conn.execute(
        """
        SELECT j.human_id, j.queue, j.agent_role, j.status AS job_status,
               r.exit_code, r.duration_ms, r.error, r.started_at, r.ended_at,
               r.created_at, r.output_ref, r.prompt_ref, r.stdout_ref,
               r.candidate_git_sha AS run_candidate_sha, j.candidate_git_sha
        FROM agent_runs r
        INNER JOIN orchestration_jobs j ON j.id = r.job_id
        WHERE j.project_human_id = ?
        ORDER BY r.id DESC
        LIMIT ?
        """,
        (project_human_id, cap),
    ).fetchall()
    return [
        {
            "job_human_id": str(row["human_id"]),
            "queue": str(row["queue"]),
            "role": str(row["agent_role"]),
            "job_status": str(row["job_status"]),
            "exit_code": int(row["exit_code"]) if row["exit_code"] is not None else None,
            "duration_ms": int(row["duration_ms"]) if row["duration_ms"] is not None else None,
            "error": row["error"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
            "candidate_git_sha": row["run_candidate_sha"] or row["candidate_git_sha"],
            "has_candidate": bool(row["run_candidate_sha"] or row["candidate_git_sha"]),
            "evidence_ref": public_artifact_ref(row["output_ref"] or row["stdout_ref"]),
            "prompt_ref": public_artifact_ref(row["prompt_ref"]),
        }
        for row in rows
    ]


def summarize_usage_for_project(
    conn: sqlite3.Connection, project_human_id: str
) -> dict[str, Any]:
    """Aggregate explicit token fields from agent_runs. Never invent totals."""
    from projectos.budget import _parse_usage

    rows = conn.execute(
        """
        SELECT r.usage_json
        FROM agent_runs r
        INNER JOIN orchestration_jobs j ON j.id = r.job_id
        WHERE j.project_human_id = ?
        """,
        (project_human_id,),
    ).fetchall()
    run_count = len(rows)
    runs_with_usage = 0
    input_tokens = 0
    output_tokens = 0
    saw_input = False
    saw_output = False
    for row in rows:
        inp, out, reported = _parse_usage(row["usage_json"])
        if not reported:
            continue
        runs_with_usage += 1
        if inp is not None:
            input_tokens += inp
            saw_input = True
        if out is not None:
            output_tokens += out
            saw_output = True
    return {
        "reported": runs_with_usage > 0,
        "input_tokens": input_tokens if saw_input else None,
        "output_tokens": output_tokens if saw_output else None,
        "runs_with_usage": runs_with_usage,
        "run_count": run_count,
    }


def list_agent_run_usage_for_project(
    conn: sqlite3.Connection, project_human_id: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Return persisted usage rows. Token totals are only present when recorded."""
    from projectos.budget import _parse_usage

    cap = max(1, min(int(limit), 200))
    rows = conn.execute(
        """
        SELECT j.human_id, j.agent_role, r.exit_code, r.duration_ms,
               r.created_at, r.usage_json
        FROM agent_runs r
        INNER JOIN orchestration_jobs j ON j.id = r.job_id
        WHERE j.project_human_id = ?
        ORDER BY r.id DESC
        LIMIT ?
        """,
        (project_human_id, cap),
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        inp, output_tokens, reported = _parse_usage(row["usage_json"])
        records.append(
            {
                "job_human_id": str(row["human_id"]),
                "role": str(row["agent_role"]),
                "exit_code": int(row["exit_code"]) if row["exit_code"] is not None else None,
                "duration_ms": int(row["duration_ms"]) if row["duration_ms"] is not None else None,
                "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
                "reported": reported,
                "input_tokens": inp,
                "output_tokens": output_tokens,
            }
        )
    return records


def get_orchestration_control(
    conn: sqlite3.Connection, project_human_id: str
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT project_human_id, paused, paused_reason, updated_at
        FROM project_orchestration_control
        WHERE project_human_id = ?
        """,
        (project_human_id,),
    ).fetchone()
    if row is None:
        return {
            "project_human_id": project_human_id,
            "paused": False,
            "paused_reason": None,
            "updated_at": None,
        }
    return {
        "project_human_id": str(row["project_human_id"]),
        "paused": bool(row["paused"]),
        "paused_reason": row["paused_reason"],
        "updated_at": row["updated_at"],
    }


def is_project_paused(conn: sqlite3.Connection, project_human_id: str) -> bool:
    return bool(get_orchestration_control(conn, project_human_id)["paused"])


def set_project_paused(
    conn: sqlite3.Connection,
    project_human_id: str,
    *,
    paused: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO project_orchestration_control (
            project_human_id, paused, paused_reason, updated_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(project_human_id) DO UPDATE SET
            paused = excluded.paused,
            paused_reason = excluded.paused_reason,
            updated_at = excluded.updated_at
        """,
        (project_human_id, 1 if paused else 0, reason if paused else None, now),
    )
    return get_orchestration_control(conn, project_human_id)


def acquire_operation_lock(
    conn: sqlite3.Connection,
    lock_name: str,
    *,
    owner: str,
    stale_seconds: int = 7200,
) -> None:
    now = utc_now()
    row = conn.execute(
        "SELECT owner, acquired_at FROM api_operation_locks WHERE lock_name = ?",
        (lock_name,),
    ).fetchone()
    if row is not None:
        acquired = parse_iso(str(row["acquired_at"]))
        if acquired is not None:
            if acquired.tzinfo is None:
                acquired = acquired.replace(tzinfo=timezone.utc)
            age = (now - acquired).total_seconds()
            if age < stale_seconds:
                raise ConflictError(
                    f"operation {lock_name} is already in progress"
                )
        conn.execute(
            "DELETE FROM api_operation_locks WHERE lock_name = ?",
            (lock_name,),
        )
    conn.execute(
        """
        INSERT INTO api_operation_locks (lock_name, owner, acquired_at)
        VALUES (?, ?, ?)
        """,
        (lock_name, owner, utc_now_iso()),
    )


def release_operation_lock(conn: sqlite3.Connection, lock_name: str) -> None:
    conn.execute(
        "DELETE FROM api_operation_locks WHERE lock_name = ?",
        (lock_name,),
    )


def get_idempotency_record(
    conn: sqlite3.Connection, scope: str, idempotency_key: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT fingerprint, status_code, response_json
        FROM api_idempotency_keys
        WHERE scope = ? AND idempotency_key = ?
        """,
        (scope, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    return {
        "fingerprint": str(row["fingerprint"]),
        "status_code": int(row["status_code"]),
        "response_json": str(row["response_json"]),
    }


def put_idempotency_record(
    conn: sqlite3.Connection,
    *,
    scope: str,
    idempotency_key: str,
    fingerprint: str,
    status_code: int,
    response_json: str,
) -> None:
    conn.execute(
        """
        INSERT INTO api_idempotency_keys (
            scope, idempotency_key, fingerprint, status_code, response_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            scope,
            idempotency_key,
            fingerprint,
            status_code,
            response_json,
            utc_now_iso(),
        ),
    )


def upsert_report_snapshot(
    conn: sqlite3.Connection, envelope: dict[str, Any]
) -> dict[str, Any]:
    """Archive a collected report envelope. Does not change jobs or QA state."""
    project = require_safe_id(str(envelope.get("project_human_id") or ""), label="project_human_id")
    kind = require_safe_id(str(envelope.get("report_kind") or ""), label="report_kind")
    revision = require_safe_id(str(envelope.get("revision") or ""), label="revision")
    snapshot_id = require_safe_id(f"RPT-{kind}-{revision[:16]}", label="snapshot_human_id")
    iteration = envelope.get("iteration_human_id")
    release_id = envelope.get("release_human_id")
    generated = str(envelope.get("generated_at") or utc_now_iso())
    saved = utc_now_iso()
    payload = json.dumps(envelope, sort_keys=True, default=str)
    conn.execute(
        """
        INSERT INTO report_snapshots (
            snapshot_human_id, project_human_id, report_kind, revision,
            iteration_human_id, release_human_id, generated_at, saved_at, envelope_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (project_human_id, report_kind, revision)
        DO UPDATE SET
            snapshot_human_id = excluded.snapshot_human_id,
            iteration_human_id = excluded.iteration_human_id,
            release_human_id = excluded.release_human_id,
            generated_at = excluded.generated_at,
            saved_at = excluded.saved_at,
            envelope_json = excluded.envelope_json
        """,
        (
            snapshot_id,
            project,
            kind,
            revision,
            iteration,
            release_id,
            generated,
            saved,
            payload,
        ),
    )
    return {
        "snapshot_human_id": snapshot_id,
        "project_human_id": project,
        "report_kind": kind,
        "revision": revision,
        "iteration_human_id": iteration,
        "release_human_id": release_id,
        "generated_at": generated,
        "saved_at": saved,
        "origin": "snapshot",
    }


def list_report_snapshots(
    conn: sqlite3.Connection, project_human_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    project = require_safe_id(project_human_id, label="project_human_id")
    cap = max(1, min(int(limit), 100))
    rows = conn.execute(
        """
        SELECT snapshot_human_id, project_human_id, report_kind, revision,
               iteration_human_id, release_human_id, generated_at, saved_at
        FROM report_snapshots
        WHERE project_human_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (project, cap),
    ).fetchall()
    return [
        {
            "snapshot_human_id": str(row["snapshot_human_id"]),
            "project_human_id": str(row["project_human_id"]),
            "report_kind": str(row["report_kind"]),
            "revision": str(row["revision"]),
            "iteration_human_id": row["iteration_human_id"],
            "release_human_id": row["release_human_id"],
            "generated_at": str(row["generated_at"]),
            "saved_at": str(row["saved_at"]),
            "origin": "snapshot",
        }
        for row in rows
    ]


def get_report_snapshot(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    snapshot_human_id: str,
) -> dict[str, Any] | None:
    project = require_safe_id(project_human_id, label="project_human_id")
    snapshot_id = require_safe_id(snapshot_human_id, label="snapshot_human_id")
    row = conn.execute(
        """
        SELECT snapshot_human_id, project_human_id, report_kind, revision,
               iteration_human_id, release_human_id, generated_at, saved_at, envelope_json
        FROM report_snapshots
        WHERE project_human_id = ? AND snapshot_human_id = ?
        """,
        (project, snapshot_id),
    ).fetchone()
    if row is None:
        return None
    envelope = json.loads(str(row["envelope_json"]))
    envelope["origin"] = "snapshot"
    envelope["snapshot_human_id"] = str(row["snapshot_human_id"])
    envelope["saved_at"] = str(row["saved_at"])
    return envelope


def _memory_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "memory_human_id": str(row["memory_human_id"]),
        "project_human_id": str(row["project_human_id"]),
        "agent_role": str(row["agent_role"]),
        "memory_kind": str(row["memory_kind"]),
        "memory_key": str(row["memory_key"]),
        "title": str(row["title"]),
        "evidence_ref": public_artifact_ref(row["evidence_ref"]),
        "source_job_human_id": row["source_job_human_id"],
        "confidence": float(row["confidence"]),
        "occurrence_count": int(row["occurrence_count"]),
        "last_validated_at": row["last_validated_at"],
        "status": str(row["status"]),
        "promotion_mode": str(row["promotion_mode"]),
        "rejection_code": row["rejection_code"],
        "rejection_reason": row["rejection_reason"],
        "superseded_by_memory_human_id": row["superseded_by_memory_human_id"]
        if "superseded_by_memory_human_id" in row.keys()
        else None,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def get_agent_memory(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    agent_role: str,
    memory_key: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM agent_memories
        WHERE project_human_id = ? AND agent_role = ? AND memory_key = ?
        """,
        (project_human_id, agent_role, memory_key),
    ).fetchone()
    return _memory_row(row) if row else None


def get_agent_memory_by_human_id(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    memory_human_id: str,
) -> dict[str, Any] | None:
    project = require_safe_id(project_human_id, label="project_human_id")
    mem_id = require_safe_id(memory_human_id, label="memory_human_id")
    row = conn.execute(
        """
        SELECT * FROM agent_memories
        WHERE project_human_id = ? AND memory_human_id = ?
        """,
        (project, mem_id),
    ).fetchone()
    return _memory_row(row) if row else None


def update_memory_governance(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    memory_human_id: str,
    status: str,
    superseded_by_memory_human_id: str | None = None,
    rejection_code: str | None = None,
    rejection_reason: str | None = None,
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    mem_id = require_safe_id(memory_human_id, label="memory_human_id")
    conn.execute(
        """
        UPDATE agent_memories
        SET status = ?,
            superseded_by_memory_human_id = ?,
            rejection_code = ?,
            rejection_reason = ?,
            updated_at = ?
        WHERE project_human_id = ? AND memory_human_id = ?
        """,
        (
            status,
            superseded_by_memory_human_id,
            rejection_code,
            rejection_reason,
            utc_now_iso(),
            project,
            mem_id,
        ),
    )
    row = get_agent_memory_by_human_id(
        conn, project_human_id=project, memory_human_id=mem_id
    )
    if row is None:
        raise OrchestrationError(f"memory {mem_id!r} not found")
    return row


def upsert_agent_memory(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    memory_human_id: str,
    agent_role: str,
    memory_kind: str = "AGENT_MEMORY",
    memory_key: str,
    title: str,
    evidence_ref: str | None,
    source_job_human_id: str | None,
    confidence: float,
    occurrence_count: int,
    last_validated_at: str | None,
    status: str,
    promotion_mode: str = "AUTO_LEARNED",
    rejection_code: str | None = None,
    rejection_reason: str | None = None,
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    mem_id = require_safe_id(memory_human_id, label="memory_human_id")
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO agent_memories (
            memory_human_id, project_human_id, agent_role, memory_kind, memory_key,
            title, evidence_ref, source_job_human_id, confidence, occurrence_count,
            last_validated_at, status, promotion_mode, rejection_code, rejection_reason,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (project_human_id, agent_role, memory_key)
        DO UPDATE SET
            title = excluded.title,
            evidence_ref = excluded.evidence_ref,
            source_job_human_id = excluded.source_job_human_id,
            confidence = excluded.confidence,
            occurrence_count = excluded.occurrence_count,
            last_validated_at = excluded.last_validated_at,
            status = excluded.status,
            memory_kind = excluded.memory_kind,
            promotion_mode = excluded.promotion_mode,
            rejection_code = excluded.rejection_code,
            rejection_reason = excluded.rejection_reason,
            updated_at = excluded.updated_at
        """,
        (
            mem_id,
            project,
            agent_role,
            memory_kind,
            memory_key,
            title,
            public_artifact_ref(evidence_ref),
            source_job_human_id,
            confidence,
            occurrence_count,
            last_validated_at,
            status,
            promotion_mode,
            rejection_code,
            rejection_reason,
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM agent_memories
        WHERE project_human_id = ? AND agent_role = ? AND memory_key = ?
        """,
        (project, agent_role, memory_key),
    ).fetchone()
    assert row is not None
    return _memory_row(row)


def append_memory_event(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    memory_human_id: str,
    event_type: str,
    job_human_id: str | None = None,
    actor: str | None = None,
    rejection_code: str | None = None,
    rejection_reason: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO agent_memory_events (
            project_human_id, memory_human_id, event_type, job_human_id,
            actor, rejection_code, rejection_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_human_id,
            memory_human_id,
            event_type,
            job_human_id,
            actor,
            rejection_code,
            rejection_reason,
            utc_now_iso(),
        ),
    )


def record_memory_injection(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    memory_human_id: str,
    job_human_id: str,
    agent_run_id: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO agent_memory_injections (
            project_human_id, memory_human_id, job_human_id, agent_run_id, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (project_human_id, memory_human_id, job_human_id, agent_run_id, utc_now_iso()),
    )


def list_agent_memories_for_project(
    conn: sqlite3.Connection, project_human_id: str, *, status: str | None = None
) -> list[dict[str, Any]]:
    if status:
        rows = conn.execute(
            """
            SELECT * FROM agent_memories
            WHERE project_human_id = ? AND status = ?
            ORDER BY last_validated_at DESC, id DESC
            """,
            (project_human_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM agent_memories
            WHERE project_human_id = ?
            ORDER BY status ASC, last_validated_at DESC, id DESC
            """,
            (project_human_id,),
        ).fetchall()
    return [_memory_row(row) for row in rows]


def list_memory_events_for_project(
    conn: sqlite3.Connection, project_human_id: str, *, limit: int | None = 50
) -> list[dict[str, Any]]:
    sql = """
        SELECT id, project_human_id, memory_human_id, event_type, job_human_id,
               actor, rejection_code, rejection_reason, created_at
        FROM agent_memory_events
        WHERE project_human_id = ?
        ORDER BY id DESC
    """
    if limit is None:
        rows = conn.execute(sql, (project_human_id,)).fetchall()
    else:
        cap = max(1, min(int(limit), 10_000))
        rows = conn.execute(sql + " LIMIT ?", (project_human_id, cap)).fetchall()
    return [
        {
            "id": int(row["id"]),
            "project_human_id": str(row["project_human_id"]),
            "memory_human_id": str(row["memory_human_id"]),
            "event_type": str(row["event_type"]),
            "job_human_id": row["job_human_id"],
            "actor": row["actor"] if "actor" in row.keys() else None,
            "rejection_code": row["rejection_code"],
            "rejection_reason": row["rejection_reason"],
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def list_memory_injections_for_project(
    conn: sqlite3.Connection, project_human_id: str, *, limit: int | None = 50
) -> list[dict[str, Any]]:
    sql = """
        SELECT id, project_human_id, memory_human_id, job_human_id, agent_run_id, created_at
        FROM agent_memory_injections
        WHERE project_human_id = ?
        ORDER BY id DESC
    """
    if limit is None:
        rows = conn.execute(sql, (project_human_id,)).fetchall()
    else:
        cap = max(1, min(int(limit), 10_000))
        rows = conn.execute(sql + " LIMIT ?", (project_human_id, cap)).fetchall()
    return [
        {
            "id": int(row["id"]),
            "project_human_id": str(row["project_human_id"]),
            "memory_human_id": str(row["memory_human_id"]),
            "job_human_id": str(row["job_human_id"]),
            "agent_run_id": int(row["agent_run_id"]) if row["agent_run_id"] is not None else None,
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def set_job_sponsor_authority(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    authority: str,
) -> OrchestrationJob:
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE orchestration_jobs
        SET sponsor_authority = ?, updated_at = ?
        WHERE id = ?
        """,
        (authority, now, job_id),
    )
    append_run_event(
        conn,
        job_id,
        "sponsor.granted",
        message=authority,
    )
    return _row_to_job(conn, job_id)


def _decision_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "decision_human_id": str(row["decision_human_id"]),
        "project_human_id": str(row["project_human_id"]),
        "action": str(row["action"]),
        "target_kind": str(row["target_kind"]),
        "target_human_id": row["target_human_id"],
        "reason": str(row["reason"]),
        "impact": str(row["impact"]),
        "requested_by": str(row["requested_by"]),
        "status": str(row["status"]),
        "decided_by": row["decided_by"],
        "decision_reason": row["decision_reason"],
        "execution_result": row["execution_result"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "decided_at": row["decided_at"],
    }


def insert_governance_decision(
    conn: sqlite3.Connection,
    *,
    decision_human_id: str,
    project_human_id: str,
    action: str,
    target_kind: str,
    target_human_id: str | None,
    reason: str,
    impact: str,
    requested_by: str,
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    hid = require_safe_id(decision_human_id, label="decision_human_id")
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO governance_decisions (
            decision_human_id, project_human_id, action, target_kind, target_human_id,
            reason, impact, requested_by, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
        """,
        (hid, project, action, target_kind, target_human_id, reason, impact, requested_by, now, now),
    )
    append_governance_decision_event(
        conn,
        project_human_id=project,
        decision_human_id=hid,
        event_type="opened",
        actor=requested_by,
        reason=reason,
    )
    row = get_governance_decision(conn, project_human_id=project, decision_human_id=hid)
    assert row is not None
    return row


def append_governance_decision_event(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    decision_human_id: str,
    event_type: str,
    actor: str | None,
    reason: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO governance_decision_events (
            project_human_id, decision_human_id, event_type, actor, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_human_id, decision_human_id, event_type, actor, reason, utc_now_iso()),
    )


def get_governance_decision(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    decision_human_id: str,
) -> dict[str, Any] | None:
    project = require_safe_id(project_human_id, label="project_human_id")
    hid = require_safe_id(decision_human_id, label="decision_human_id")
    foreign = conn.execute(
        """
        SELECT project_human_id FROM governance_decisions
        WHERE decision_human_id = ?
        """,
        (hid,),
    ).fetchone()
    if foreign is not None and str(foreign["project_human_id"]) != project:
        raise CrossProjectWriteError(
            "governance decision belongs to "
            f"{foreign['project_human_id']!r}, not {project!r}"
        )
    row = conn.execute(
        """
        SELECT * FROM governance_decisions
        WHERE project_human_id = ? AND decision_human_id = ?
        """,
        (project, hid),
    ).fetchone()
    return _decision_row(row) if row else None


def list_governance_decisions_for_project(
    conn: sqlite3.Connection,
    project_human_id: str,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    project = require_safe_id(project_human_id, label="project_human_id")
    if status:
        rows = conn.execute(
            """
            SELECT * FROM governance_decisions
            WHERE project_human_id = ? AND status = ?
            ORDER BY id DESC
            """,
            (project, status),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM governance_decisions
            WHERE project_human_id = ?
            ORDER BY status ASC, id DESC
            """,
            (project,),
        ).fetchall()
    return [_decision_row(row) for row in rows]


def list_governance_decision_events(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    decision_human_id: str,
) -> list[dict[str, Any]]:
    project = require_safe_id(project_human_id, label="project_human_id")
    hid = require_safe_id(decision_human_id, label="decision_human_id")
    rows = conn.execute(
        """
        SELECT project_human_id, decision_human_id, event_type, actor, reason, created_at
        FROM governance_decision_events
        WHERE project_human_id = ? AND decision_human_id = ?
        ORDER BY id ASC
        """,
        (project, hid),
    ).fetchall()
    return [
        {
            "project_human_id": str(row["project_human_id"]),
            "decision_human_id": str(row["decision_human_id"]),
            "event_type": str(row["event_type"]),
            "actor": row["actor"],
            "reason": row["reason"],
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def find_open_governance_decision(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    action: str,
    target_human_id: str | None,
) -> dict[str, Any] | None:
    project = require_safe_id(project_human_id, label="project_human_id")
    row = conn.execute(
        """
        SELECT * FROM governance_decisions
        WHERE project_human_id = ? AND action = ? AND status = 'OPEN'
          AND IFNULL(target_human_id, '') = IFNULL(?, '')
        ORDER BY id DESC
        LIMIT 1
        """,
        (project, action, target_human_id),
    ).fetchone()
    return _decision_row(row) if row else None


def update_governance_decision(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    decision_human_id: str,
    status: str,
    decided_by: str,
    decision_reason: str,
    execution_result: str | None,
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    hid = require_safe_id(decision_human_id, label="decision_human_id")
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE governance_decisions
        SET status = ?,
            decided_by = ?,
            decision_reason = ?,
            execution_result = ?,
            decided_at = ?,
            updated_at = ?
        WHERE project_human_id = ? AND decision_human_id = ?
        """,
        (status, decided_by, decision_reason, execution_result, now, now, project, hid),
    )
    row = get_governance_decision(conn, project_human_id=project, decision_human_id=hid)
    if row is None:
        raise OrchestrationError(f"decision {hid!r} not found")
    return row


def _slack_binding_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "binding_human_id": str(row["binding_human_id"]),
        "project_human_id": str(row["project_human_id"]),
        "team_id": str(row["team_id"] or ""),
        "channel_id": str(row["channel_id"]),
        "thread_ts": str(row["thread_ts"] or ""),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def insert_slack_binding(
    conn: sqlite3.Connection,
    *,
    binding_human_id: str,
    project_human_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> dict[str, Any]:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO slack_bindings (
            binding_human_id, project_human_id, team_id, channel_id, thread_ts,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (binding_human_id, project_human_id, team_id, channel_id, thread_ts, now, now),
    )
    row = get_slack_binding(conn, team_id=team_id, channel_id=channel_id, thread_ts=thread_ts)
    assert row is not None
    return row


def get_slack_binding(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM slack_bindings
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
        """,
        (team_id, channel_id, thread_ts),
    ).fetchone()
    return _slack_binding_row(row) if row else None


def list_slack_bindings_for_project(
    conn: sqlite3.Connection, project_human_id: str
) -> list[dict[str, Any]]:
    project = require_safe_id(project_human_id, label="project_human_id")
    rows = conn.execute(
        """
        SELECT * FROM slack_bindings
        WHERE project_human_id = ?
        ORDER BY channel_id ASC, thread_ts ASC
        """,
        (project,),
    ).fetchall()
    return [_slack_binding_row(row) for row in rows]


def delete_slack_binding(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> None:
    conn.execute(
        """
        DELETE FROM slack_bindings
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
        """,
        (team_id, channel_id, thread_ts),
    )


def insert_slack_message_ref(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    message_ts: str,
) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO slack_message_refs (
            project_human_id, team_id, channel_id, thread_ts, message_ts, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, channel_id, message_ts) DO NOTHING
        """,
        (project_human_id, team_id, channel_id, thread_ts, message_ts, now),
    )


def list_slack_message_refs_for_project(
    conn: sqlite3.Connection, project_human_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    project = require_safe_id(project_human_id, label="project_human_id")
    cap = max(1, min(int(limit), 200))
    rows = conn.execute(
        """
        SELECT project_human_id, team_id, channel_id, thread_ts, message_ts, created_at
        FROM slack_message_refs
        WHERE project_human_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (project, cap),
    ).fetchall()
    return [
        {
            "project_human_id": str(row["project_human_id"]),
            "team_id": str(row["team_id"] or "") or None,
            "channel_id": str(row["channel_id"]),
            "thread_ts": str(row["thread_ts"] or "") or None,
            "message_ts": str(row["message_ts"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def _slack_binding_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "binding_human_id": str(row["binding_human_id"]),
        "project_human_id": str(row["project_human_id"]),
        "team_id": str(row["team_id"] or ""),
        "channel_id": str(row["channel_id"]),
        "thread_ts": str(row["thread_ts"] or ""),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def insert_slack_binding(
    conn: sqlite3.Connection,
    *,
    binding_human_id: str,
    project_human_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> dict[str, Any]:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO slack_bindings (
            binding_human_id, project_human_id, team_id, channel_id, thread_ts,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (binding_human_id, project_human_id, team_id, channel_id, thread_ts, now, now),
    )
    row = get_slack_binding(conn, team_id=team_id, channel_id=channel_id, thread_ts=thread_ts)
    assert row is not None
    return row


def get_slack_binding(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM slack_bindings
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
        """,
        (team_id, channel_id, thread_ts),
    ).fetchone()
    return _slack_binding_row(row) if row else None


def list_slack_bindings_for_project(
    conn: sqlite3.Connection, project_human_id: str
) -> list[dict[str, Any]]:
    project = require_safe_id(project_human_id, label="project_human_id")
    rows = conn.execute(
        """
        SELECT * FROM slack_bindings
        WHERE project_human_id = ?
        ORDER BY channel_id ASC, thread_ts ASC
        """,
        (project,),
    ).fetchall()
    return [_slack_binding_row(row) for row in rows]


def delete_slack_binding(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> None:
    conn.execute(
        """
        DELETE FROM slack_bindings
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
        """,
        (team_id, channel_id, thread_ts),
    )


def insert_slack_message_ref(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    message_ts: str,
) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO slack_message_refs (
            project_human_id, team_id, channel_id, thread_ts, message_ts, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, channel_id, message_ts) DO NOTHING
        """,
        (project_human_id, team_id, channel_id, thread_ts, message_ts, now),
    )


def list_slack_message_refs_for_project(
    conn: sqlite3.Connection, project_human_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    project = require_safe_id(project_human_id, label="project_human_id")
    cap = max(1, min(int(limit), 200))
    rows = conn.execute(
        """
        SELECT project_human_id, team_id, channel_id, thread_ts, message_ts, created_at
        FROM slack_message_refs
        WHERE project_human_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (project, cap),
    ).fetchall()
    return [
        {
            "project_human_id": str(row["project_human_id"]),
            "team_id": str(row["team_id"] or "") or None,
            "channel_id": str(row["channel_id"]),
            "thread_ts": str(row["thread_ts"] or "") or None,
            "message_ts": str(row["message_ts"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]

def get_slack_intake_item(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    message_ts: str,
    item_kind: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT project_human_id, item_kind, item_human_id, team_id, channel_id,
               thread_ts, message_ts, created_at
        FROM slack_intake_items
        WHERE team_id = ? AND channel_id = ? AND message_ts = ? AND item_kind = ?
        """,
        (team_id, channel_id, message_ts, item_kind),
    ).fetchone()
    if row is None:
        return None
    return {
        "project_human_id": str(row["project_human_id"]),
        "item_kind": str(row["item_kind"]),
        "item_human_id": str(row["item_human_id"]),
        "team_id": str(row["team_id"] or "") or None,
        "channel_id": str(row["channel_id"]),
        "thread_ts": str(row["thread_ts"] or "") or None,
        "message_ts": str(row["message_ts"]),
        "created_at": str(row["created_at"]),
    }


def insert_slack_intake_item(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    item_kind: str,
    item_human_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    message_ts: str,
) -> dict[str, Any]:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO slack_intake_items (
            project_human_id, item_kind, item_human_id, team_id, channel_id,
            thread_ts, message_ts, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_human_id,
            item_kind,
            item_human_id,
            team_id,
            channel_id,
            thread_ts,
            message_ts,
            now,
        ),
    )
    found = get_slack_intake_item(
        conn,
        team_id=team_id,
        channel_id=channel_id,
        message_ts=message_ts,
        item_kind=item_kind,
    )
    assert found is not None
    return found


def _slack_notification_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "notification_human_id": str(row["notification_human_id"]),
        "project_human_id": str(row["project_human_id"]),
        "kind": str(row["kind"]),
        "entity_human_id": str(row["entity_human_id"]),
        "channel_id": str(row["channel_id"]),
        "team_id": str(row["team_id"] or "") or None,
        "thread_ts": str(row["thread_ts"] or "") or None,
        "text": str(row["text"]),
        "dashboard_path": str(row["dashboard_path"]),
        "created_at": str(row["created_at"]),
    }


def list_slack_notifications_for_project(
    conn: sqlite3.Connection, project_human_id: str
) -> list[dict[str, Any]]:
    project = require_safe_id(project_human_id, label="project_human_id")
    rows = conn.execute(
        """
        SELECT notification_human_id, project_human_id, kind, entity_human_id,
               channel_id, team_id, thread_ts, text, dashboard_path, created_at
        FROM slack_notifications
        WHERE project_human_id = ?
        ORDER BY id DESC
        """,
        (project,),
    ).fetchall()
    return [_slack_notification_row(row) for row in rows]


def insert_slack_notification(
    conn: sqlite3.Connection,
    *,
    notification_human_id: str,
    project_human_id: str,
    kind: str,
    entity_human_id: str,
    channel_id: str,
    team_id: str,
    thread_ts: str,
    text: str,
    dashboard_path: str,
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    hid = require_safe_id(notification_human_id, label="notification_human_id")
    entity = require_safe_id(entity_human_id, label="entity_human_id")
    now = utc_now_iso()
    try:
        conn.execute(
            """
            INSERT INTO slack_notifications (
                notification_human_id, project_human_id, kind, entity_human_id,
                channel_id, team_id, thread_ts, text, dashboard_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hid,
                project,
                kind,
                entity,
                channel_id,
                team_id or "",
                thread_ts or "",
                text,
                dashboard_path,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        row = conn.execute(
            """
            SELECT notification_human_id, project_human_id, kind, entity_human_id,
                   channel_id, team_id, thread_ts, text, dashboard_path, created_at
            FROM slack_notifications
            WHERE project_human_id = ? AND kind = ? AND entity_human_id = ?
            """,
            (project, kind, entity),
        ).fetchone()
        if row is None:
            raise
        return _slack_notification_row(row)
    row = conn.execute(
        """
        SELECT notification_human_id, project_human_id, kind, entity_human_id,
               channel_id, team_id, thread_ts, text, dashboard_path, created_at
        FROM slack_notifications
        WHERE notification_human_id = ?
        """,
        (hid,),
    ).fetchone()
    assert row is not None
    return _slack_notification_row(row)


def list_all_slack_bindings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT binding_human_id, project_human_id, team_id, channel_id, thread_ts,
               created_at, updated_at
        FROM slack_bindings
        ORDER BY project_human_id, channel_id
        """
    ).fetchall()
    return [_slack_binding_row(row) for row in rows]


def claim_slack_envelope(conn: sqlite3.Connection, envelope_id: str, payload_type: str = "") -> bool:
    """Return True if this envelope is new and now claimed. False if already processed."""
    eid = str(envelope_id or "").strip()
    if not eid:
        return False
    try:
        conn.execute(
            """
            INSERT INTO slack_socket_envelopes (envelope_id, payload_type)
            VALUES (?, ?)
            """,
            (eid, str(payload_type or "")[:80]),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def claim_slack_event(
    conn: sqlite3.Connection,
    dedup_key: str,
    *,
    team_id: str = "",
    channel_id: str = "",
    message_ts: str = "",
    event_id: str = "",
) -> bool:
    """Return True when this logical Slack event is new. False if already processed."""
    key = str(dedup_key or "").strip()
    if not key:
        return False
    try:
        conn.execute(
            """
            INSERT INTO slack_event_dedup (
                dedup_key, team_id, channel_id, message_ts, event_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                key,
                str(team_id or "")[:80],
                str(channel_id or "")[:80],
                str(message_ts or "")[:80],
                str(event_id or "")[:120],
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def claim_slack_events(
    conn: sqlite3.Connection,
    dedup_keys: list[str],
    *,
    team_id: str = "",
    channel_id: str = "",
    message_ts: str = "",
    event_id: str = "",
) -> bool:
    """Claim every key for this delivery. False when any key was already processed."""
    keys = [str(key or "").strip() for key in dedup_keys if str(key or "").strip()]
    if not keys:
        return False
    try:
        with conn:
            for key in keys:
                conn.execute(
                    """
                    INSERT INTO slack_event_dedup (
                        dedup_key, team_id, channel_id, message_ts, event_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        str(team_id or "")[:80],
                        str(channel_id or "")[:80],
                        str(message_ts or "")[:80],
                        str(event_id or "")[:120],
                    ),
                )
    except sqlite3.IntegrityError:
        return False
    return True


def release_slack_events(conn: sqlite3.Connection, dedup_keys: list[str]) -> None:
    """Release durable claims so Slack can retry after an unhandled processing failure."""
    keys = [str(key or "").strip() for key in dedup_keys if str(key or "").strip()]
    if not keys:
        return
    with conn:
        for key in keys:
            conn.execute("DELETE FROM slack_event_dedup WHERE dedup_key = ?", (key,))


def _slack_interface_channel_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "channel_id": row["channel_id"],
        "team_id": row["team_id"] or "",
        "is_default": bool(row["is_default"]),
        "created_at": row["created_at"],
    }


def list_slack_interface_channels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT channel_id, team_id, is_default, created_at
        FROM slack_interface_channels
        ORDER BY is_default DESC, channel_id
        """
    ).fetchall()
    return [_slack_interface_channel_row(row) for row in rows]


def is_slack_interface_channel(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    team_id: str = "",
) -> bool:
    channel = str(channel_id or "").strip()
    team = str(team_id or "").strip()
    row = conn.execute(
        """
        SELECT 1 FROM slack_interface_channels
        WHERE channel_id = ? AND (team_id = '' OR team_id = ?)
        LIMIT 1
        """,
        (channel, team),
    ).fetchone()
    return row is not None


def add_slack_interface_channel(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    team_id: str = "",
    is_default: bool = False,
) -> dict[str, Any]:
    channel = require_safe_id(channel_id, label="channel_id")
    team = str(team_id or "").strip()
    if is_default:
        conn.execute("UPDATE slack_interface_channels SET is_default = 0")
    conn.execute(
        """
        INSERT INTO slack_interface_channels (channel_id, team_id, is_default)
        VALUES (?, ?, ?)
        ON CONFLICT(team_id, channel_id) DO UPDATE SET
            is_default = excluded.is_default
        """,
        (channel, team, 1 if is_default else 0),
    )
    row = conn.execute(
        """
        SELECT channel_id, team_id, is_default, created_at
        FROM slack_interface_channels
        WHERE channel_id = ? AND team_id = ?
        """,
        (channel, team),
    ).fetchone()
    assert row is not None
    return _slack_interface_channel_row(row)


def remove_slack_interface_channel(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    team_id: str = "",
) -> bool:
    channel = require_safe_id(channel_id, label="channel_id")
    team = str(team_id or "").strip()
    cursor = conn.execute(
        "DELETE FROM slack_interface_channels WHERE channel_id = ? AND team_id = ?",
        (channel, team),
    )
    return cursor.rowcount > 0


def set_default_slack_interface_channel(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    team_id: str = "",
) -> dict[str, Any] | None:
    channel = require_safe_id(channel_id, label="channel_id")
    team = str(team_id or "").strip()
    row = conn.execute(
        """
        SELECT channel_id FROM slack_interface_channels
        WHERE channel_id = ? AND team_id = ?
        """,
        (channel, team),
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE slack_interface_channels SET is_default = 0")
    conn.execute(
        """
        UPDATE slack_interface_channels SET is_default = 1
        WHERE channel_id = ? AND team_id = ?
        """,
        (channel, team),
    )
    updated = conn.execute(
        """
        SELECT channel_id, team_id, is_default, created_at
        FROM slack_interface_channels
        WHERE channel_id = ? AND team_id = ?
        """,
        (channel, team),
    ).fetchone()
    return _slack_interface_channel_row(updated) if updated else None


def _slack_project_context_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "team_id": row["team_id"] or "",
        "channel_id": row["channel_id"],
        "thread_ts": row["thread_ts"] or "",
        "user_id": row["user_id"],
        "project_human_id": row["project_human_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
    }


def get_slack_project_context(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    user_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT team_id, channel_id, thread_ts, user_id, project_human_id,
               created_at, updated_at, expires_at
        FROM slack_project_context
        WHERE team_id = ? AND channel_id = ? AND thread_ts = ? AND user_id = ?
        """,
        (
            str(team_id or "").strip(),
            str(channel_id or "").strip(),
            str(thread_ts or "").strip(),
            str(user_id or "").strip(),
        ),
    ).fetchone()
    return _slack_project_context_row(row) if row else None


def upsert_slack_project_context(
    conn: sqlite3.Connection,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    user_id: str,
    project_human_id: str,
    expires_at: str | None = None,
) -> dict[str, Any]:
    team = str(team_id or "").strip()
    channel = require_safe_id(channel_id, label="channel_id")
    thread = str(thread_ts or "").strip()
    user = require_safe_id(user_id, label="user_id")
    project = require_safe_id(project_human_id, label="project_human_id")
    conn.execute(
        """
        INSERT INTO slack_project_context (
            team_id, channel_id, thread_ts, user_id, project_human_id, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, channel_id, thread_ts, user_id) DO UPDATE SET
            project_human_id = excluded.project_human_id,
            updated_at = datetime('now'),
            expires_at = excluded.expires_at
        """,
        (team, channel, thread, user, project, expires_at),
    )
    row = get_slack_project_context(
        conn,
        team_id=team,
        channel_id=channel,
        thread_ts=thread,
        user_id=user,
    )
    assert row is not None
    return row


