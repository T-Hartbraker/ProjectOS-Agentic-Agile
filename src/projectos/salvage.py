"""Governed salvage of a delivery candidate left in a worktree after FAILED."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from projectos.db import connection
from projectos.errors import OrchestrationError
from projectos.migrate import initialize_database
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.qa_handoff import create_assurance_jobs_for_delivery
from projectos.registry import load_registry
from projectos.store import (
    OrchestrationJob,
    active_lease_for_job,
    append_run_event,
    find_active_worktree_holder,
    get_job,
    get_job_by_human_id,
    utc_now_iso,
)
from projectos.worktree import (
    common_git_dir,
    current_head_sha,
    is_ancestor,
    is_dirty,
    resolve_worktree_entry,
)


@dataclass(frozen=True)
class SalvageResult:
    job_human_id: str
    status: str
    outcome: str | None
    attempt: int
    base_git_sha: str | None
    candidate_git_sha: str | None
    worktree_path: str | None
    assurance_job_ids: list[str] = field(default_factory=list)
    message: str = ""
    exit_code: int = 0
    already_salvaged: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _paths_equal(a: Path | str | None, b: Path | str | None) -> bool:
    if a is None or b is None:
        return False
    left = Path(a)
    right = Path(b)
    try:
        return left.resolve().samefile(right.resolve())
    except OSError:
        return left.resolve() == right.resolve()


def _fail(job: OrchestrationJob | None, message: str) -> SalvageResult:
    return SalvageResult(
        job_human_id=job.human_id if job else "",
        status=job.status if job else "UNKNOWN",
        outcome=job.outcome if job else None,
        attempt=job.attempt if job else 0,
        base_git_sha=job.base_git_sha if job else None,
        candidate_git_sha=job.candidate_git_sha if job else None,
        worktree_path=job.worktree_path if job else None,
        message=message,
        exit_code=1,
    )


def _validate_salvage_preconditions(
    conn,
    job: OrchestrationJob,
    *,
    registry_path: Path,
) -> tuple[str, Path] | SalvageResult:
    """Return (candidate_sha, worktree_path) or a failure SalvageResult."""
    if job.queue != "DELIVERY" or job.agent_role != "DELIVERY":
        return _fail(job, "salvage refused: job is not a DELIVERY job")

    if job.outcome == "SALVAGED" and job.status == "SUCCEEDED" and job.candidate_git_sha:
        # Idempotent path checked by caller after HEAD resolve.
        pass
    elif job.status != "FAILED":
        return _fail(
            job,
            f"salvage refused: job status must be FAILED (got {job.status})",
        )

    if not job.project_human_id:
        return _fail(job, "salvage refused: missing project identity")
    if not job.repository_root:
        return _fail(job, "salvage refused: missing repository_root")
    if not job.worktree_path or not job.worktree_name:
        return _fail(job, "salvage refused: missing persisted worktree")
    if not job.base_git_sha:
        return _fail(job, "salvage refused: missing persisted base_git_sha")

    registry = load_registry(registry_path)
    entry = registry.get(job.project_human_id)
    if entry is None:
        return _fail(
            job,
            f"salvage refused: project {job.project_human_id!r} not in registry",
        )
    if not _paths_equal(entry.repository_root, job.repository_root):
        return _fail(
            job,
            "salvage refused: repository identity mismatch "
            f"(job={job.repository_root!r} registry={entry.repository_root!r})",
        )

    repo = Path(job.repository_root).resolve()
    worktree = Path(job.worktree_path).resolve()
    if not worktree.is_dir():
        return _fail(job, f"salvage refused: worktree path missing: {worktree}")

    entry_wt = resolve_worktree_entry(repo, worktree)
    if entry_wt is None:
        return _fail(
            job,
            "salvage refused: worktree is not registered in "
            "git worktree list --porcelain",
        )

    try:
        if common_git_dir(worktree) != common_git_dir(repo):
            return _fail(
                job,
                "salvage refused: worktree does not share repository identity",
            )
    except Exception as exc:  # noqa: BLE001 — fail closed on git identity errors
        return _fail(job, f"salvage refused: repository identity check failed: {exc}")

    if is_dirty(worktree):
        return _fail(job, "salvage refused: worktree is dirty")

    head = current_head_sha(worktree)
    if not head:
        return _fail(job, "salvage refused: unable to resolve worktree HEAD")

    base = job.base_git_sha.strip()
    if head == base:
        return _fail(job, "salvage refused: HEAD equals base_git_sha (no candidate)")

    if not is_ancestor(worktree, base, head):
        return _fail(
            job,
            "salvage refused: HEAD does not descend from persisted base_git_sha",
        )

    holder = find_active_worktree_holder(
        conn, job.worktree_name, exclude_job_id=job.id
    )
    if holder is not None:
        return _fail(
            job,
            "salvage refused: live ProjectOS-owned worker still owns worktree "
            f"({holder.human_id} status={holder.status})",
        )

    if job.status in {"LEASED", "RUNNING"} or active_lease_for_job(conn, job.id):
        return _fail(
            job,
            "salvage refused: job still has an active ProjectOS worker lease",
        )

    if job.candidate_git_sha and job.candidate_git_sha != head:
        return _fail(
            job,
            "salvage refused: ambiguous provenance "
            f"(persisted candidate {job.candidate_git_sha} != HEAD {head})",
        )

    porcelain_head = (entry_wt.get("HEAD") or "").strip()
    if porcelain_head and porcelain_head != head:
        return _fail(
            job,
            "salvage refused: ambiguous provenance "
            f"(worktree list HEAD {porcelain_head} != rev-parse HEAD {head})",
        )

    return head, worktree


def salvage_delivery_candidate(
    *,
    job_human_id: str,
    db_path: Path | None = None,
    registry_path: Path | None = None,
) -> SalvageResult:
    """Salvage a committed worktree candidate after a FAILED DELIVERY run.

    Preserves FAILED attempt/event history. Records outcome=SALVAGED and creates
    new assurance jobs bound to the salvaged candidate SHA. Does not dispatch.
    """
    db = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    registry = (
        Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    )
    initialize_database(db)

    with connection(db) as conn:
        job = get_job_by_human_id(conn, job_human_id)
        if job is None:
            return _fail(None, f"salvage refused: job {job_human_id!r} not found")

        validated = _validate_salvage_preconditions(
            conn, job, registry_path=registry
        )
        if isinstance(validated, SalvageResult):
            return validated
        candidate_sha, worktree = validated

        if (
            job.outcome == "SALVAGED"
            and job.status == "SUCCEEDED"
            and job.candidate_git_sha == candidate_sha
        ):
            existing = conn.execute(
                """
                SELECT human_id FROM orchestration_jobs
                WHERE source_delivery_job_id = ?
                  AND source_candidate_sha = ?
                ORDER BY id
                """,
                (job.id, candidate_sha),
            ).fetchall()
            return SalvageResult(
                job_human_id=job.human_id,
                status=job.status,
                outcome=job.outcome,
                attempt=job.attempt,
                base_git_sha=job.base_git_sha,
                candidate_git_sha=job.candidate_git_sha,
                worktree_path=job.worktree_path,
                assurance_job_ids=[str(r[0]) for r in existing],
                message=(
                    "already salvaged; candidate and assurance lineage unchanged"
                ),
                exit_code=0,
                already_salvaged=True,
            )

        # Re-check status gate for non-idempotent path (FAILED only).
        if job.status != "FAILED":
            return _fail(
                job,
                f"salvage refused: job status must be FAILED (got {job.status})",
            )

        attempt_before = job.attempt
        now = utc_now_iso()
        conn.execute(
            """
            UPDATE orchestration_jobs
            SET status = 'SUCCEEDED',
                outcome = 'SALVAGED',
                candidate_git_sha = ?,
                last_error = NULL,
                completed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (candidate_sha, now, now, job.id),
        )
        append_run_event(
            conn,
            job.id,
            "delivery.candidate_salvaged",
            status="SUCCEEDED",
            message=(
                "Governed salvage of worktree candidate after FAILED execution; "
                "prior failure history preserved"
            ),
            payload={
                "candidate_git_sha": candidate_sha,
                "base_git_sha": job.base_git_sha,
                "worktree_path": str(worktree),
                "worktree_name": job.worktree_name,
                "attempt_preserved": attempt_before,
                "previous_status": "FAILED",
                "previous_error": job.last_error,
                "outcome": "SALVAGED",
            },
        )

        salvaged = get_job(conn, job.id)
        if salvaged.attempt != attempt_before:
            raise OrchestrationError(
                "salvage aborted: attempt counter must be preserved"
            )

        handoff = create_assurance_jobs_for_delivery(
            conn,
            salvaged,
            candidate_git_sha=candidate_sha,
        )

        final = get_job(conn, job.id)
        return SalvageResult(
            job_human_id=final.human_id,
            status=final.status,
            outcome=final.outcome,
            attempt=final.attempt,
            base_git_sha=final.base_git_sha,
            candidate_git_sha=final.candidate_git_sha,
            worktree_path=final.worktree_path,
            assurance_job_ids=list(handoff.assurance_job_ids),
            message=(
                "Salvaged candidate from worktree; FAILED history preserved; "
                "new assurance jobs created (not dispatched)"
            ),
            exit_code=0,
        )
