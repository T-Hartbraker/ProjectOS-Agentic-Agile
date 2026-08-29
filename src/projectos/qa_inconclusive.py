"""Schedule durable assurance retries for inconclusive QA results."""

from __future__ import annotations

import sqlite3

from projectos.constants import ASSURANCE_QUEUES
from projectos.domain_events import ACTOR_PM, EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.qa_handoff import create_assurance_retry
from projectos.run_next_actions import persist_run_next_action


def _inconclusive_roles(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    project_id: str,
    candidate_git_sha: str,
    roles: list[str] | None,
) -> list[tuple[str, int, int | None]]:
    """Return (queue, producer_job_id, prior_assurance_job_id) for each retry target."""

    def _delivery_producer() -> int | None:
        row = conn.execute(
            """
            SELECT id FROM orchestration_jobs
            WHERE project_human_id = ?
              AND candidate_git_sha = ?
              AND queue = 'DELIVERY'
              AND status = 'SUCCEEDED'
            ORDER BY id DESC LIMIT 1
            """,
            (project_id, candidate_git_sha),
        ).fetchone()
        return int(row["id"]) if row else None

    if roles:
        target_roles = roles
        producer = _delivery_producer()
        if producer is None:
            return []
        return [(role, producer, None) for role in target_roles]

    rows = conn.execute(
        """
        SELECT assurance_role, delivery_job_id, assurance_job_id
        FROM qa_evidence
        WHERE run_id = ? AND candidate_git_sha = ? AND result = 'inconclusive'
        ORDER BY id ASC
        """,
        (run_id, candidate_git_sha),
    ).fetchall()
    if rows:
        resolved: list[tuple[str, int, int | None]] = []
        fallback_producer = _delivery_producer()
        for r in rows:
            producer_id = r["delivery_job_id"]
            if producer_id is None:
                producer_id = fallback_producer
            if producer_id is None:
                continue
            resolved.append(
                (
                    str(r["assurance_role"]),
                    int(producer_id),
                    int(r["assurance_job_id"]) if r["assurance_job_id"] else None,
                )
            )
        if resolved:
            return resolved

    producer = _delivery_producer()
    if producer is None:
        return []
    return [(role, producer, None) for role in ASSURANCE_QUEUES]


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
    if not candidate_git_sha:
        raise OrchestrationError("INCONCLUSIVE retry requires candidate_git_sha")
    targets = _inconclusive_roles(
        conn,
        run_id=run_id,
        project_id=project_id,
        candidate_git_sha=candidate_git_sha,
        roles=inconclusive_roles,
    )
    if not targets:
        raise OrchestrationError("No producer lineage found for inconclusive assurance retry")

    created_ids: list[int] = []
    for role, producer_job_id, prior_job_id in targets:
        job = create_assurance_retry(
            conn,
            run_id=run_id,
            project_id=project_id,
            repository_root=repository_root,
            candidate_git_sha=candidate_git_sha,
            assessor_queue=role,
            producer_job_id=producer_job_id,
            prior_assurance_job_id=prior_job_id,
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
                "producer_job_id": producer_job_id,
            },
        )
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
            "roles": [t[0] for t in targets],
        },
    )
    return created_ids
