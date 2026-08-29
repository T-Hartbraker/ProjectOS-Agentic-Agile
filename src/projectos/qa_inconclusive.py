"""Schedule durable assurance retries for inconclusive QA results."""

from __future__ import annotations

import sqlite3

from projectos.constants import ASSURANCE_QUEUES, QUEUE_TO_ROLE
from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.run_next_actions import persist_run_next_action
from projectos.store import create_job, get_job_by_human_id


def schedule_assurance_retry_for_inconclusive(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    project_id: str,
    repository_root: str,
    candidate_git_sha: str,
    run_id: str,
    inconclusive_roles: list[str] | None = None,
) -> list[int]:
    """Create READY assurance retry jobs on the same candidate and durable next actions."""
    roles = inconclusive_roles or list(ASSURANCE_QUEUES)
    created_ids: list[int] = []
    anchor = f"{run_id}__INCONCLUSIVE__{candidate_git_sha[:12]}"
    for role in roles:
        human_id = f"{anchor}__{role}__RETRY"
        existing = get_job_by_human_id(conn, human_id)
        if existing is not None:
            if existing.status in {"READY", "LEASED", "RUNNING", "RETRY_WAIT"}:
                created_ids.append(existing.id)
            continue
        job = create_job(
            conn,
            human_id=human_id,
            project_human_id=project_id,
            repository_root=repository_root,
            agent_role=QUEUE_TO_ROLE.get(role, role),
            queue=role,
            status="READY",
            base_git_sha=candidate_git_sha,
        )
        created_ids.append(job.id)
        persist_run_next_action(
            conn,
            run_id=run_id,
            project_id=project_id,
            action_type="ACTIVE_ASSESSMENT",
            orchestration_job_id=job.id,
            payload={
                "reason": "qa_inconclusive",
                "candidate_git_sha": candidate_git_sha,
                "assurance_role": role,
            },
        )
    if not created_ids:
        raise OrchestrationError("No assurance retry jobs could be scheduled for inconclusive QA")
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="ASSURANCE_RETRY_SCHEDULED",
        summary="PM scheduled durable assurance retry jobs for inconclusive QA gate.",
        actor_id=ACTOR_PM,
        phase="QA_GATE",
        evidence={
            "candidate_git_sha": candidate_git_sha,
            "assurance_job_ids": created_ids,
            "roles": roles,
        },
    )
    return created_ids
