"""Bind RELEASE jobs to the integrated candidate they must evaluate."""

from __future__ import annotations

from projectos.errors import OrchestrationError
from projectos.qa_handoff import REQUIRED_ASSURANCE
from projectos.store import (
    OrchestrationJob,
    append_run_event,
    get_job,
    get_job_by_human_id,
    job_satisfies_dependency,
    list_dependent_job_ids,
    list_job_dependencies,
    promote_queued_to_ready,
    set_job_source_provenance,
)

_INVALID_OUTCOMES = frozenset({"INVALIDATED", "SUPERSEDED", "NO_CHANGE"})


def _identity_matches(integration: OrchestrationJob, release: OrchestrationJob) -> bool:
    if integration.project_human_id != release.project_human_id:
        return False
    return integration.iteration_human_id == release.iteration_human_id


def _delivery_qa_passed(conn, delivery: OrchestrationJob) -> bool:
    candidate = delivery.candidate_git_sha
    if not candidate:
        return False
    rows = conn.execute(
        """
        SELECT assurance_role, result FROM qa_evidence
        WHERE delivery_job_id = ? AND candidate_git_sha = ?
        """,
        (delivery.id, candidate),
    ).fetchall()
    by_role = {str(row[0]): row[1] for row in rows}
    for role in REQUIRED_ASSURANCE:
        if by_role.get(role) != "pass":
            return False
    manager = get_job_by_human_id(conn, f"{delivery.human_id}__QA_MANAGER")
    return manager is not None and manager.status == "SUCCEEDED"


def integration_qa_gates_satisfied(conn, integration: OrchestrationJob) -> bool:
    """True when every DELIVERY predecessor of INTEGRATION has passing QA."""
    for dep_id in list_job_dependencies(conn, integration.id):
        dep = get_job(conn, dep_id)
        if dep.queue != "DELIVERY":
            continue
        if not job_satisfies_dependency(dep):
            return False
        if not _delivery_qa_passed(conn, dep):
            return False
    return True


def integration_allows_release(
    conn, integration: OrchestrationJob, release: OrchestrationJob
) -> bool:
    if integration.queue != "INTEGRATION":
        return False
    if integration.status != "SUCCEEDED":
        return False
    if integration.outcome in _INVALID_OUTCOMES:
        return False
    if not integration.candidate_git_sha:
        return False
    if not _identity_matches(integration, release):
        return False
    return integration_qa_gates_satisfied(conn, integration)


def release_prerequisites_met(conn, release: OrchestrationJob) -> bool:
    """True when RELEASE may become or remain runnable."""
    if release.queue != "RELEASE":
        return True
    for dep_id in list_job_dependencies(conn, release.id):
        dep = get_job(conn, dep_id)
        if dep.queue != "INTEGRATION":
            continue
        if integration_allows_release(conn, dep, release):
            return True
    return False


def resolve_integrated_candidate(
    conn, job: OrchestrationJob
) -> tuple[int | None, str | None]:
    """Return (integration_job_id, sha) a RELEASE job must assess."""
    found: dict[str, int] = {}
    for dep_id in list_job_dependencies(conn, job.id):
        dep = get_job(conn, dep_id)
        if dep.queue != "INTEGRATION":
            continue
        if dep.status != "SUCCEEDED":
            continue
        if dep.outcome in _INVALID_OUTCOMES:
            continue
        if not dep.candidate_git_sha:
            continue
        if not _identity_matches(dep, job):
            continue
        found[dep.candidate_git_sha] = dep.id
    if len(found) > 1:
        raise OrchestrationError(
            "RELEASE refused: ambiguous integrated candidates "
            f"{sorted(found)}"
        )
    if len(found) == 1:
        sha, integ_id = next(iter(found.items()))
        return integ_id, sha
    if job.source_candidate_sha:
        return job.source_delivery_job_id, job.source_candidate_sha
    return None, None


def bind_release_provenance(conn, job: OrchestrationJob) -> OrchestrationJob:
    """Persist INTEGRATION candidate SHA onto a RELEASE job before it runs."""
    if job.queue != "RELEASE":
        return job
    if not release_prerequisites_met(conn, job):
        raise OrchestrationError(
            "RELEASE refused: missing integrated candidate provenance "
            "or unmet QA/identity gates "
            "(no SUCCEEDED INTEGRATION candidate_git_sha)"
        )
    integ_id, sha = resolve_integrated_candidate(conn, job)
    if not sha:
        raise OrchestrationError(
            "RELEASE refused: missing integrated candidate provenance "
            "(no SUCCEEDED INTEGRATION candidate_git_sha)"
        )
    if (
        job.source_candidate_sha == sha
        and job.source_delivery_job_id == integ_id
    ):
        return job
    updated = set_job_source_provenance(
        conn,
        job.id,
        source_delivery_job_id=integ_id,
        source_candidate_sha=sha,
    )
    append_run_event(
        conn,
        job.id,
        "release.candidate_bound",
        status=job.status,
        message=f"Bound RELEASE to integrated candidate {sha}",
        payload={
            "source_integration_job_id": integ_id,
            "source_candidate_sha": sha,
        },
    )
    return updated


def _bind_sha_from_integration(
    conn, release: OrchestrationJob, integration: OrchestrationJob
) -> None:
    if not integration.candidate_git_sha:
        return
    if (
        release.source_candidate_sha == integration.candidate_git_sha
        and release.source_delivery_job_id == integration.id
    ):
        return
    set_job_source_provenance(
        conn,
        release.id,
        source_delivery_job_id=integration.id,
        source_candidate_sha=integration.candidate_git_sha,
    )
    append_run_event(
        conn,
        release.id,
        "release.candidate_bound",
        status=release.status,
        message=(
            "Bound RELEASE to integrated candidate "
            f"{integration.candidate_git_sha}"
        ),
        payload={
            "source_integration_job_id": integration.id,
            "source_candidate_sha": integration.candidate_git_sha,
        },
    )


def promote_eligible_release_jobs(
    conn,
    *,
    project_human_id: str | None = None,
    iteration_human_id: str | None = None,
) -> list[str]:
    """Promote QUEUED RELEASE jobs whose integration/QA/identity gates pass."""
    clauses = ["queue = 'RELEASE'", "status = 'QUEUED'"]
    params: list[object] = []
    if project_human_id is not None:
        clauses.append("project_human_id = ?")
        params.append(project_human_id)
    if iteration_human_id is not None:
        clauses.append("iteration_human_id = ?")
        params.append(iteration_human_id)
    rows = conn.execute(
        f"""
        SELECT id FROM orchestration_jobs
        WHERE {' AND '.join(clauses)}
        ORDER BY id ASC
        """,
        params,
    ).fetchall()
    promoted: list[str] = []
    for row in rows:
        rel = get_job(conn, int(row[0]))
        if not release_prerequisites_met(conn, rel):
            continue
        bind_release_provenance(conn, rel)
        updated = promote_queued_to_ready(
            conn,
            rel.id,
            reason=(
                "RELEASE promoted to READY after INTEGRATION SUCCEEDED "
                "with a valid candidate and satisfied QA gates"
            ),
        )
        if updated.status == "READY":
            promoted.append(updated.human_id)
    return promoted


def bind_dependent_release_jobs(
    conn, integration: OrchestrationJob
) -> list[str]:
    """Bind INTEGRATION SHA onto matching RELEASE jobs and promote if eligible."""
    if integration.queue != "INTEGRATION":
        return []
    if integration.status != "SUCCEEDED" or not integration.candidate_git_sha:
        return []
    if integration.outcome in _INVALID_OUTCOMES:
        return []
    bound: list[str] = []
    for dep_job_id in list_dependent_job_ids(conn, integration.id):
        rel = get_job(conn, dep_job_id)
        if rel.queue != "RELEASE":
            continue
        if rel.status not in {"READY", "QUEUED"}:
            continue
        if not _identity_matches(integration, rel):
            continue
        _bind_sha_from_integration(conn, rel, integration)
        bound.append(rel.human_id)
    promote_eligible_release_jobs(
        conn,
        project_human_id=integration.project_human_id,
        iteration_human_id=integration.iteration_human_id,
    )
    return bound
