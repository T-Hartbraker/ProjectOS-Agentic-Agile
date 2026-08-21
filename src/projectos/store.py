"""Orchestration job and lease persistence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from projectos.errors import LeaseError, OrchestrationError
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


def get_job(conn: sqlite3.Connection, job_id: int) -> OrchestrationJob:
    return _row_to_job(conn, job_id)


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
               j.attempt, j.max_attempts
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
