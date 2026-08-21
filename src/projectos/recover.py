"""ProjectOS recovery: expired leases, identity revalidation, worktree reconcile."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from projectos.db import connection
from projectos.errors import OrchestrationError, ProjectOSError
from projectos.migrate import initialize_database
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.prompt_builder import resolve_delivery_assignment
from projectos.registry import load_registry
from projectos.store import (
    OrchestrationJob,
    append_run_event,
    get_job,
    get_job_by_human_id,
    list_jobs_by_statuses,
    list_jobs_for_project,
    mark_blocked,
    mark_ready_from_blocked,
    promote_retry_wait_to_ready,
    recover_expired_leases,
    set_job_assignment,
)
from projectos.validation import validate_registry_entry
from projectos.worktree import common_git_dir, list_worktrees

# Non-terminal jobs that recovery may touch for identity / worktree checks.
RECOVERY_ACTIVE_STATUSES = frozenset(
    {"QUEUED", "READY", "LEASED", "RUNNING", "RETRY_WAIT"}
)

_REVALIDATABLE_ERROR_MARKERS = (
    "acceptance criteria are empty",
    "lacks resolvable work-item context",
    "Cannot resolve work item",
)


@dataclass
class IdentityCheckResult:
    project_human_id: str
    ok: bool
    repository_root: str | None
    error: str | None = None


@dataclass
class WorktreeReconcileResult:
    job_human_id: str
    action: str
    message: str


@dataclass
class RecoveryReport:
    expired_lease_job_ids: list[int] = field(default_factory=list)
    promoted_ready: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    identity_checks: list[IdentityCheckResult] = field(default_factory=list)
    worktree_actions: list[WorktreeReconcileResult] = field(default_factory=list)
    unknown_worktrees_ignored: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked and all(c.ok for c in self.identity_checks)


@dataclass
class RevalidateBlockedResult:
    job_human_id: str
    status: str
    message: str
    previous_error: str | None = None
    acceptance_criteria_count: int = 0
    created_duplicate: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "READY"

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1


def _paths_equal(a: str | Path, b: str | Path) -> bool:
    return Path(a).resolve() == Path(b).resolve()


def _is_revalidatable_block_error(error: str | None) -> bool:
    if not error:
        return False
    return any(marker in error for marker in _REVALIDATABLE_ERROR_MARKERS)


def _check_project_identity(
    project_human_id: str,
    *,
    expected_repository_root: str,
    registry_path: Path,
    projectctl_runner=None,
) -> IdentityCheckResult:
    """Revalidate registry identity; never rewrite job repository bindings."""
    try:
        registry = load_registry(registry_path)
        entry = registry.get(project_human_id)
        if entry is None:
            return IdentityCheckResult(
                project_human_id=project_human_id,
                ok=False,
                repository_root=expected_repository_root,
                error=f"Project {project_human_id} is not in the registry",
            )
        if not _paths_equal(entry.repository_root, expected_repository_root):
            return IdentityCheckResult(
                project_human_id=project_human_id,
                ok=False,
                repository_root=expected_repository_root,
                error=(
                    "Identity drift: registry repository_root "
                    f"{entry.repository_root} does not match job-bound "
                    f"{expected_repository_root}. Refusing to move job."
                ),
            )
        validated = validate_registry_entry(
            entry, projectctl_runner=projectctl_runner
        )
        if validated.identity.project_human_id != project_human_id:
            return IdentityCheckResult(
                project_human_id=project_human_id,
                ok=False,
                repository_root=expected_repository_root,
                error=(
                    "Identity drift: repository.json project_human_id "
                    f"{validated.identity.project_human_id} != {project_human_id}"
                ),
            )
        if not _paths_equal(validated.git_root, expected_repository_root):
            return IdentityCheckResult(
                project_human_id=project_human_id,
                ok=False,
                repository_root=expected_repository_root,
                error=(
                    "Identity drift: git root "
                    f"{validated.git_root} does not match job-bound "
                    f"{expected_repository_root}"
                ),
            )
        return IdentityCheckResult(
            project_human_id=project_human_id,
            ok=True,
            repository_root=str(Path(expected_repository_root).resolve()),
        )
    except ProjectOSError as exc:
        return IdentityCheckResult(
            project_human_id=project_human_id,
            ok=False,
            repository_root=expected_repository_root,
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return IdentityCheckResult(
            project_human_id=project_human_id,
            ok=False,
            repository_root=expected_repository_root,
            error=str(exc),
        )


def revalidate_blocked_job(
    *,
    job_human_id: str,
    db_path: Path | str | None = None,
    registry_path: Path | str | None = None,
    projectctl_runner=None,
    show_work_item_fn=None,
) -> RevalidateBlockedResult:
    """Revalidate a BLOCKED job against its persisted identity bindings.

    Never substitutes another project/repository/work item. Does not create
    duplicate jobs. Preserves historical BLOCKED events.
    """
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    reg_path = (
        Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    )
    initialize_database(path)

    with connection(path) as conn:
        job = get_job_by_human_id(conn, job_human_id)
        if job is None:
            return RevalidateBlockedResult(
                job_human_id=job_human_id,
                status="error",
                message=f"Job {job_human_id!r} not found",
            )
        if job.status != "BLOCKED":
            return RevalidateBlockedResult(
                job_human_id=job_human_id,
                status=job.status,
                message=(
                    f"Job {job_human_id} is {job.status}, not BLOCKED; "
                    "refusing generic unblock"
                ),
                previous_error=job.last_error,
            )
        if not _is_revalidatable_block_error(job.last_error):
            append_run_event(
                conn,
                job.id,
                "job.revalidate_refused",
                status="BLOCKED",
                message=(
                    "BLOCKED error is not a revalidatable work-item predicate; "
                    "refusing generic unblock"
                ),
                payload={"last_error": job.last_error},
            )
            return RevalidateBlockedResult(
                job_human_id=job_human_id,
                status="BLOCKED",
                message=(
                    "Refusing to revalidate: last_error is not a work-item / "
                    f"acceptance-criteria blocker ({job.last_error!r})"
                ),
                previous_error=job.last_error,
            )

        before_wi_type = job.work_item_type
        before_wi_id = job.work_item_human_id
        before_root = job.repository_root
        before_project = job.project_human_id
        before_base = job.base_git_sha
        previous_error = job.last_error
        jobs_before = conn.execute(
            "SELECT COUNT(*) FROM orchestration_jobs"
        ).fetchone()[0]

        identity = _check_project_identity(
            job.project_human_id,
            expected_repository_root=job.repository_root,
            registry_path=reg_path,
            projectctl_runner=projectctl_runner,
        )
        if not identity.ok:
            append_run_event(
                conn,
                job.id,
                "job.revalidate_failed",
                status="BLOCKED",
                message=identity.error or "identity validation failed",
                payload={
                    "reason": "identity_drift",
                    "previous_error": previous_error,
                },
            )
            conn.execute(
                """
                UPDATE orchestration_jobs
                SET last_error = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (identity.error or "identity validation failed", job.id),
            )
            return RevalidateBlockedResult(
                job_human_id=job_human_id,
                status="BLOCKED",
                message=identity.error or "identity validation failed",
                previous_error=previous_error,
            )

        if job.queue != "DELIVERY":
            append_run_event(
                conn,
                job.id,
                "job.revalidate_refused",
                status="BLOCKED",
                message="Only DELIVERY work-item blockers are revalidated here",
            )
            return RevalidateBlockedResult(
                job_human_id=job_human_id,
                status="BLOCKED",
                message="Refusing to revalidate non-DELIVERY blocked job",
                previous_error=previous_error,
            )

        if not (job.work_item_type and job.work_item_human_id):
            append_run_event(
                conn,
                job.id,
                "job.revalidate_failed",
                status="BLOCKED",
                message="Persisted work-item identity is missing",
                payload={"previous_error": previous_error},
            )
            return RevalidateBlockedResult(
                job_human_id=job_human_id,
                status="BLOCKED",
                message="Persisted work-item identity is missing; cannot revalidate",
                previous_error=previous_error,
            )

        python_executable = None
        try:
            registry = load_registry(reg_path)
            entry = registry.get(job.project_human_id)
            if entry is not None:
                validated = validate_registry_entry(
                    entry, projectctl_runner=projectctl_runner
                )
                python_executable = validated.projectctl_python
        except ProjectOSError:
            python_executable = None

        resolve_kwargs = {
            "repository_root": Path(job.repository_root),
            "python_executable": python_executable,
        }
        try:
            if show_work_item_fn is not None:
                import projectos.prompt_builder as pb

                original = pb.show_work_item
                pb.show_work_item = show_work_item_fn  # type: ignore[assignment]
                try:
                    resolved = resolve_delivery_assignment(job, **resolve_kwargs)
                finally:
                    pb.show_work_item = original
            else:
                resolved = resolve_delivery_assignment(job, **resolve_kwargs)
        except (OrchestrationError, ProjectOSError) as exc:
            append_run_event(
                conn,
                job.id,
                "job.revalidate_failed",
                status="BLOCKED",
                message=str(exc),
                payload={
                    "previous_error": previous_error,
                    "work_item_type": before_wi_type,
                    "work_item_human_id": before_wi_id,
                },
            )
            conn.execute(
                """
                UPDATE orchestration_jobs
                SET last_error = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (str(exc), job.id),
            )
            return RevalidateBlockedResult(
                job_human_id=job_human_id,
                status="BLOCKED",
                message=str(exc),
                previous_error=previous_error,
            )

        if (
            resolved.work_item_type != before_wi_type
            or resolved.work_item_human_id != before_wi_id
        ):
            msg = (
                "Revalidation attempted to change work-item identity; refused "
                f"(persisted {before_wi_type}/{before_wi_id}, "
                f"resolved {resolved.work_item_type}/{resolved.work_item_human_id})"
            )
            append_run_event(
                conn,
                job.id,
                "job.revalidate_failed",
                status="BLOCKED",
                message=msg,
            )
            return RevalidateBlockedResult(
                job_human_id=job_human_id,
                status="BLOCKED",
                message=msg,
                previous_error=previous_error,
            )

        assignment: dict = {}
        if job.assignment_json:
            try:
                loaded = json.loads(job.assignment_json)
            except json.JSONDecodeError:
                loaded = {}
            if isinstance(loaded, dict):
                assignment = loaded
        assignment.update(
            {
                "title": resolved.title,
                "acceptance_criteria": list(resolved.acceptance_criteria),
                "requirement_ref": resolved.requirement_ref
                or f"{before_wi_type}:{before_wi_id}",
            }
        )
        set_job_assignment(conn, job.id, assignment)

        refreshed = mark_ready_from_blocked(
            conn,
            job.id,
            reason=(
                "Revalidated work-item assignment; acceptance criteria now "
                f"resolvable ({len(resolved.acceptance_criteria)} criteria)"
            ),
        )

        assert refreshed.work_item_type == before_wi_type
        assert refreshed.work_item_human_id == before_wi_id
        assert refreshed.repository_root == before_root
        assert refreshed.project_human_id == before_project
        assert refreshed.base_git_sha == before_base
        assert refreshed.status == "READY"

        jobs_after = conn.execute(
            "SELECT COUNT(*) FROM orchestration_jobs"
        ).fetchone()[0]
        return RevalidateBlockedResult(
            job_human_id=job_human_id,
            status="READY",
            message=(
                f"Revalidated {job_human_id}: BLOCKED -> READY "
                f"({len(resolved.acceptance_criteria)} acceptance criteria)"
            ),
            previous_error=previous_error,
            acceptance_criteria_count=len(resolved.acceptance_criteria),
            created_duplicate=jobs_after != jobs_before,
        )


def _reconcile_job_worktree(
    conn,
    job: OrchestrationJob,
    *,
    known_paths_by_repo: dict[str, set[Path]],
) -> tuple[WorktreeReconcileResult, list[str]]:
    """Reconcile one job's recorded worktree; never adopt unknowns."""
    ignored: list[str] = []
    repo_key = str(Path(job.repository_root).resolve())

    try:
        git_entries = list_worktrees(Path(job.repository_root))
    except ProjectOSError as exc:
        append_run_event(
            conn,
            job.id,
            "worktree.reconcile_failed",
            status=job.status,
            message=str(exc),
        )
        return (
            WorktreeReconcileResult(
                job_human_id=job.human_id,
                action="error",
                message=str(exc),
            ),
            ignored,
        )

    git_paths: dict[Path, dict[str, str]] = {}
    for entry in git_entries:
        if "worktree" not in entry:
            continue
        git_paths[Path(entry["worktree"]).resolve()] = entry

    known = known_paths_by_repo.setdefault(repo_key, set())
    if job.worktree_path:
        known.add(Path(job.worktree_path).resolve())

    # Report unknown worktrees for this repository (do not adopt).
    for path in git_paths:
        if path == Path(job.repository_root).resolve():
            continue
        if path not in known and all(
            path != Path(j.worktree_path).resolve()
            for j in [job]
            if j.worktree_path
        ):
            # Defer global unknown detection to caller with full known set.
            pass

    if not job.worktree_path and not job.worktree_name:
        return (
            WorktreeReconcileResult(
                job_human_id=job.human_id,
                action="skipped",
                message="No recorded worktree",
            ),
            ignored,
        )

    if job.worktree_path:
        recorded = Path(job.worktree_path).resolve()
        if recorded in git_paths:
            try:
                repo_common = common_git_dir(Path(job.repository_root))
                wt_common = common_git_dir(recorded)
            except ProjectOSError as exc:
                append_run_event(
                    conn,
                    job.id,
                    "worktree.reconcile_mismatch",
                    status=job.status,
                    message=str(exc),
                )
                return (
                    WorktreeReconcileResult(
                        job_human_id=job.human_id,
                        action="mismatch",
                        message=str(exc),
                    ),
                    ignored,
                )
            if wt_common != repo_common:
                msg = (
                    f"Recorded worktree {recorded} belongs to {wt_common}, "
                    f"not job repository {repo_common}. Not relocating job."
                )
                append_run_event(
                    conn,
                    job.id,
                    "worktree.wrong_repository",
                    status=job.status,
                    message=msg,
                )
                return (
                    WorktreeReconcileResult(
                        job_human_id=job.human_id,
                        action="wrong_repository",
                        message=msg,
                    ),
                    ignored,
                )
            # Matched: preserve candidate_git_sha / base_git_sha / path as-is.
            head = git_paths[recorded].get("HEAD")
            append_run_event(
                conn,
                job.id,
                "worktree.reconciled",
                status=job.status,
                message="Recorded worktree matched git worktree list",
                payload={
                    "worktree_path": str(recorded),
                    "git_head": head,
                    "candidate_git_sha_preserved": job.candidate_git_sha,
                    "base_git_sha_preserved": job.base_git_sha,
                },
            )
            return (
                WorktreeReconcileResult(
                    job_human_id=job.human_id,
                    action="matched",
                    message=f"Worktree matched at {recorded}",
                ),
                ignored,
            )

        # Missing recorded path: never adopt another worktree by name.
        same_name = [
            p
            for p in git_paths
            if job.worktree_name and p.name == job.worktree_name
        ]
        msg = (
            f"Recorded worktree path {recorded} not present in "
            f"`git worktree list` for {job.repository_root}."
        )
        if same_name:
            msg += (
                f" Found path(s) with same name {same_name}; "
                "not adopting automatically."
            )
        append_run_event(
            conn,
            job.id,
            "worktree.missing",
            status=job.status,
            message=msg,
            payload={
                "recorded_path": str(recorded),
                "candidate_git_sha_preserved": job.candidate_git_sha,
                "same_name_candidates_ignored": [str(p) for p in same_name],
            },
        )
        return (
            WorktreeReconcileResult(
                job_human_id=job.human_id,
                action="missing",
                message=msg,
            ),
            ignored,
        )

    # Name recorded without path: do not auto-bind any discovered worktree.
    msg = (
        f"Job records worktree_name={job.worktree_name!r} without path; "
        "not adopting any discovered worktree automatically."
    )
    append_run_event(
        conn,
        job.id,
        "worktree.not_adopted",
        status=job.status,
        message=msg,
    )
    return (
        WorktreeReconcileResult(
            job_human_id=job.human_id,
            action="not_adopted",
            message=msg,
        ),
        ignored,
    )


def _collect_unknown_worktrees(
    jobs: list[OrchestrationJob],
) -> list[str]:
    """List git worktrees not recorded on any job; never adopt them."""
    ignored: list[str] = []
    by_repo: dict[Path, list[OrchestrationJob]] = {}
    for job in jobs:
        by_repo.setdefault(Path(job.repository_root).resolve(), []).append(job)

    for repo_root, repo_jobs in by_repo.items():
        try:
            entries = list_worktrees(repo_root)
        except ProjectOSError:
            continue
        recorded: set[Path] = {repo_root}
        for job in repo_jobs:
            if job.worktree_path:
                recorded.add(Path(job.worktree_path).resolve())
        for entry in entries:
            if "worktree" not in entry:
                continue
            path = Path(entry["worktree"]).resolve()
            if path not in recorded:
                ignored.append(str(path))
    return ignored


def run_recovery(
    *,
    db_path: Path | str | None = None,
    registry_path: Path | str | None = None,
    projectctl_runner=None,
    promote_retry_wait: bool = True,
) -> RecoveryReport:
    """Run full ProjectOS recovery pass."""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    reg_path = (
        Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    )
    initialize_database(path)
    report = RecoveryReport()

    with connection(path) as conn:
        recovered_ids = recover_expired_leases(conn)
        report.expired_lease_job_ids = list(recovered_ids)
        if recovered_ids:
            report.messages.append(
                f"Recovered {len(recovered_ids)} expired lease(s)"
            )

        active_jobs = list_jobs_by_statuses(conn, RECOVERY_ACTIVE_STATUSES)
        recovered_jobs = [get_job(conn, jid) for jid in recovered_ids]

        identity_ok_projects: set[str] = set()
        identity_bad: dict[str, str] = {}

        seen_pairs: set[tuple[str, str]] = set()
        for job in active_jobs + recovered_jobs:
            pair = (
                job.project_human_id,
                str(Path(job.repository_root).resolve()),
            )
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            check = _check_project_identity(
                job.project_human_id,
                expected_repository_root=job.repository_root,
                registry_path=reg_path,
                projectctl_runner=projectctl_runner,
            )
            report.identity_checks.append(check)
            if check.ok:
                identity_ok_projects.add(pair[0])
            else:
                identity_bad[pair[0]] = check.error or "identity validation failed"
                report.messages.append(
                    f"Identity drift for {pair[0]}: {check.error}"
                )

        # Block active jobs whose project identity drifted; never rewrite roots.
        for project_id, error in identity_bad.items():
            for job in list_jobs_for_project(
                conn, project_id, statuses=RECOVERY_ACTIVE_STATUSES
            ):
                # Preserve provenance fields by using mark_blocked (no candidate clear).
                before = get_job(conn, job.id)
                candidate = before.candidate_git_sha
                base = before.base_git_sha
                root = before.repository_root
                blocked = mark_blocked(
                    conn,
                    job.id,
                    error=error,
                    release_reason="identity_drift",
                )
                assert blocked.repository_root == root
                assert blocked.candidate_git_sha == candidate
                assert blocked.base_git_sha == base
                report.blocked.append(blocked.human_id)

        if promote_retry_wait:
            for job in list_jobs_by_statuses(conn, {"RETRY_WAIT"}):
                if job.project_human_id in identity_bad:
                    continue
                if job.project_human_id not in identity_ok_projects:
                    # Project had no successful check (e.g. empty) — re-check pair.
                    check = _check_project_identity(
                        job.project_human_id,
                        expected_repository_root=job.repository_root,
                        registry_path=reg_path,
                        projectctl_runner=projectctl_runner,
                    )
                    if not check.ok:
                        continue
                promoted = promote_retry_wait_to_ready(
                    conn,
                    job.id,
                    reason="recovery: identity OK; RETRY_WAIT promoted to READY",
                )
                report.promoted_ready.append(promoted.human_id)

        # Worktree reconciliation against each job's own repository only.
        reconcile_jobs = list_jobs_by_statuses(
            conn,
            RECOVERY_ACTIVE_STATUSES | {"BLOCKED", "FAILED", "SUCCEEDED"},
        )
        # Limit reconcile to jobs that record worktree metadata or were recovered.
        targets = [
            j
            for j in reconcile_jobs
            if j.worktree_name or j.worktree_path or j.id in set(recovered_ids)
        ]
        known_paths_by_repo: dict[str, set[Path]] = {}
        for job in targets:
            if job.worktree_path:
                known_paths_by_repo.setdefault(
                    str(Path(job.repository_root).resolve()), set()
                ).add(Path(job.worktree_path).resolve())

        for job in targets:
            # Refresh job row after possible status changes.
            job = get_job(conn, job.id)
            result, _ = _reconcile_job_worktree(
                conn, job, known_paths_by_repo=known_paths_by_repo
            )
            report.worktree_actions.append(result)

        unknowns = _collect_unknown_worktrees(reconcile_jobs)
        for path in unknowns:
            report.unknown_worktrees_ignored.append(path)
            report.messages.append(
                f"Ignored unknown worktree (not adopted): {path}"
            )

    return report
