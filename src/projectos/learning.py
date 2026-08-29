"""Organizational learning. Safe AGENT_MEMORY is auto-learned, not approved."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from projectos.errors import CrossProjectWriteError, OrchestrationError
from projectos.store import (
    append_memory_event,
    get_agent_memory,
    get_agent_memory_by_human_id,
    get_job_by_human_id,
    list_agent_memories_for_project,
    list_memory_events_for_project,
    list_memory_injections_for_project,
    public_artifact_ref,
    record_memory_injection,
    require_safe_id,
    update_memory_governance,
    upsert_agent_memory,
    utc_now_iso,
)

SAFE_KIND = "AGENT_MEMORY"
AUTO_LEARNED = "AUTO_LEARNED"
ACTIVE = "ACTIVE"
REJECTED = "REJECTED"
RETIRED = "RETIRED"
SUPERSEDED = "SUPERSEDED"
_PATH_MARKERS = ("/", "\\", "..")

_UNSAFE_TITLE = re.compile(r"(\.\.|[/\\]|[A-Za-z]:)")


def memory_key_for(project_human_id: str, agent_role: str, title: str) -> str:
    normalized = " ".join(title.strip().split()).casefold()
    digest = hashlib.sha256(
        f"{project_human_id}|{agent_role}|{normalized}".encode("utf-8")
    ).hexdigest()
    return digest[:16]


def format_memory_context(memories: list[dict[str, Any]]) -> str | None:
    if not memories:
        return None
    lines = [
        "Auto-learned AGENT_MEMORY (not a sponsor approval):",
    ]
    for item in memories:
        evidence = item.get("evidence_ref") or "Not reported"
        lines.append(
            f"- {item['memory_human_id']} [{item['agent_role']}] "
            f"confidence={item['confidence']:.2f} occurrences={item['occurrence_count']}: "
            f"{item['title']} (evidence {evidence})"
        )
    return "\n".join(lines)


def list_active_memories_for_prompt(
    conn, project_human_id: str, agent_role: str, *, limit: int = 8
) -> list[dict[str, Any]]:
    rows = list_agent_memories_for_project(conn, project_human_id, status=ACTIVE)
    matched = [row for row in rows if row["agent_role"] == agent_role]
    return matched[: max(1, min(int(limit), 20))] if matched else []


def ingest_agent_memory(
    conn,
    *,
    project_human_id: str,
    agent_role: str,
    title: str,
    memory_kind: str = SAFE_KIND,
    evidence_ref: str | None = None,
    source_job_human_id: str | None = None,
    confidence: float | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    role = str(agent_role or "").strip()
    heading = " ".join(str(title or "").split())
    if not heading:
        raise OrchestrationError("memory title is required")
    if source_job_human_id:
        job = get_job_by_human_id(conn, source_job_human_id)
        if job is None:
            raise OrchestrationError(f"job {source_job_human_id!r} not found")
        if job.project_human_id != project:
            raise CrossProjectWriteError(
                "ingest_agent_memory refused: job belongs to "
                f"{job.project_human_id!r}, not {project!r}"
            )
    evidence = public_artifact_ref(evidence_ref)
    key = memory_key_for(project, role, heading)
    existing = get_agent_memory(
        conn, project_human_id=project, agent_role=role, memory_key=key
    )
    now = utc_now_iso()
    reported_confidence = 0.5 if confidence is None else max(0.0, min(1.0, float(confidence)))

    if memory_kind != SAFE_KIND:
        return _reject(
            conn,
            project=project,
            agent_role=role,
            memory_key=key,
            title=heading,
            evidence=evidence,
            source_job_human_id=source_job_human_id,
            existing=existing,
            code="not_agent_memory",
            reason="Only AGENT_MEMORY is auto-learned",
        )
    if _UNSAFE_TITLE.search(heading):
        return _reject(
            conn,
            project=project,
            agent_role=role,
            memory_key=key,
            title=heading,
            evidence=evidence,
            source_job_human_id=source_job_human_id,
            existing=existing,
            code="unsafe_content",
            reason="Memory title looks like a filesystem path",
        )

    if existing and existing["status"] == ACTIVE:
        confidence_value = max(existing["confidence"], reported_confidence)
        row = upsert_agent_memory(
            conn,
            project_human_id=project,
            memory_human_id=existing["memory_human_id"],
            agent_role=role,
            memory_kind=SAFE_KIND,
            memory_key=key,
            title=heading,
            evidence_ref=evidence or existing.get("evidence_ref"),
            source_job_human_id=source_job_human_id or existing.get("source_job_human_id"),
            confidence=min(1.0, confidence_value),
            occurrence_count=existing["occurrence_count"] + 1,
            last_validated_at=now,
            status=ACTIVE,
            promotion_mode=AUTO_LEARNED,
        )
        append_memory_event(
            conn,
            project_human_id=project,
            memory_human_id=row["memory_human_id"],
            event_type="reinforced",
            job_human_id=source_job_human_id,
            actor=actor,
        )
        return row

    memory_id = existing["memory_human_id"] if existing else f"MEM-{key}"
    row = upsert_agent_memory(
        conn,
        project_human_id=project,
        memory_human_id=memory_id,
        agent_role=role,
        memory_kind=SAFE_KIND,
        memory_key=key,
        title=heading,
        evidence_ref=evidence,
        source_job_human_id=source_job_human_id,
        confidence=reported_confidence,
        occurrence_count=1,
        last_validated_at=now,
        status=ACTIVE,
        promotion_mode=AUTO_LEARNED,
    )
    append_memory_event(
        conn,
        project_human_id=project,
        memory_human_id=row["memory_human_id"],
        event_type="promoted",
        job_human_id=source_job_human_id,
        actor=actor,
    )
    return row


def reinforce_memories(
    conn,
    memories: list[dict[str, Any]],
    *,
    job_human_id: str | None,
) -> None:
    now = utc_now_iso()
    for item in memories:
        if item.get("status") != ACTIVE:
            continue
        row = upsert_agent_memory(
            conn,
            project_human_id=item["project_human_id"],
            memory_human_id=item["memory_human_id"],
            agent_role=item["agent_role"],
            memory_kind=SAFE_KIND,
            memory_key=item["memory_key"],
            title=item["title"],
            evidence_ref=item.get("evidence_ref"),
            source_job_human_id=job_human_id or item.get("source_job_human_id"),
            confidence=min(1.0, float(item["confidence"]) + 0.05),
            occurrence_count=int(item["occurrence_count"]) + 1,
            last_validated_at=now,
            status=ACTIVE,
            promotion_mode=AUTO_LEARNED,
        )
        append_memory_event(
            conn,
            project_human_id=item["project_human_id"],
            memory_human_id=row["memory_human_id"],
            event_type="reinforced",
            job_human_id=job_human_id,
        )


def record_injections(
    conn,
    *,
    project_human_id: str,
    job_human_id: str,
    agent_run_id: int | None,
    memories: list[dict[str, Any]],
) -> None:
    for item in memories:
        record_memory_injection(
            conn,
            project_human_id=project_human_id,
            memory_human_id=item["memory_human_id"],
            job_human_id=job_human_id,
            agent_run_id=agent_run_id,
        )


def learning_view(conn, project_human_id: str) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    memories = list_agent_memories_for_project(conn, project)
    active = [row for row in memories if row["status"] == ACTIVE]
    rejected = [row for row in memories if row["status"] == REJECTED]
    retired = [row for row in memories if row["status"] == RETIRED]
    superseded = [row for row in memories if row["status"] == SUPERSEDED]
    return {
        "project_human_id": project,
        "notice": (
            "Safe AGENT_MEMORY is auto-learned. Ordinary promotion is not an "
            "approval task. Operators may retire or supersede stale memories "
            "with confirmation, reason, and actor; history is preserved."
        ),
        "active_memories": active,
        "rejected_memories": rejected,
        "retired_memories": retired,
        "superseded_memories": superseded,
        "events": list_memory_events_for_project(conn, project),
        "injected_in_recent_runs": list_memory_injections_for_project(conn, project),
    }


def require_admin_confirmation(*, confirmed: bool, reason: str, actor: str) -> tuple[str, str]:
    if not confirmed:
        raise OrchestrationError("confirmation is required")
    reason_text = " ".join(str(reason or "").split())
    if not reason_text:
        raise OrchestrationError("reason is required")
    if any(marker in reason_text for marker in _PATH_MARKERS):
        raise OrchestrationError("reason must not contain a path")
    actor_text = str(actor or "").strip()
    if not actor_text:
        raise OrchestrationError("actor is required")
    if len(actor_text) > 128 or any(marker in actor_text for marker in _PATH_MARKERS):
        raise OrchestrationError("actor must not contain a path")
    return reason_text, actor_text


def _admin_result(
    *,
    project_human_id: str,
    action: str,
    actor: str,
    reason: str,
    memory: dict[str, Any],
    successor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "project_human_id": project_human_id,
        "action": action,
        "actor": actor,
        "reason": reason,
        "memory": memory,
        "successor": successor,
    }


def retire_memory(
    conn,
    *,
    project_human_id: str,
    memory_human_id: str,
    confirmed: bool,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    mem_id = require_safe_id(memory_human_id, label="memory_human_id")
    reason_text, actor_text = require_admin_confirmation(
        confirmed=confirmed, reason=reason, actor=actor
    )
    existing = get_agent_memory_by_human_id(
        conn, project_human_id=project, memory_human_id=mem_id
    )
    if existing is None:
        raise OrchestrationError(f"memory {mem_id!r} not found")
    if existing["status"] != ACTIVE:
        raise OrchestrationError(
            f"memory {mem_id!r} is {existing['status']} and cannot be retired"
        )
    row = update_memory_governance(
        conn,
        project_human_id=project,
        memory_human_id=mem_id,
        status=RETIRED,
        rejection_code="retired",
        rejection_reason=reason_text,
    )
    append_memory_event(
        conn,
        project_human_id=project,
        memory_human_id=mem_id,
        event_type="retired",
        actor=actor_text,
        rejection_code="retired",
        rejection_reason=reason_text,
    )
    return _admin_result(
        project_human_id=project,
        action="retire",
        actor=actor_text,
        reason=reason_text,
        memory=row,
    )


def supersede_memory(
    conn,
    *,
    project_human_id: str,
    memory_human_id: str,
    successor_title: str,
    confirmed: bool,
    reason: str,
    actor: str,
    evidence_ref: str | None = None,
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    mem_id = require_safe_id(memory_human_id, label="memory_human_id")
    reason_text, actor_text = require_admin_confirmation(
        confirmed=confirmed, reason=reason, actor=actor
    )
    heading = " ".join(str(successor_title or "").split())
    if not heading:
        raise OrchestrationError("successor_title is required")
    if _UNSAFE_TITLE.search(heading):
        raise OrchestrationError("successor_title must not contain a path")
    existing = get_agent_memory_by_human_id(
        conn, project_human_id=project, memory_human_id=mem_id
    )
    if existing is None:
        raise OrchestrationError(f"memory {mem_id!r} not found")
    if existing["status"] != ACTIVE:
        raise OrchestrationError(
            f"memory {mem_id!r} is {existing['status']} and cannot be superseded"
        )
    if heading.casefold() == str(existing["title"]).casefold():
        raise OrchestrationError("successor_title must differ from the current title")
    successor = ingest_agent_memory(
        conn,
        project_human_id=project,
        agent_role=existing["agent_role"],
        title=heading,
        memory_kind=SAFE_KIND,
        evidence_ref=public_artifact_ref(evidence_ref),
        source_job_human_id=existing.get("source_job_human_id"),
        confidence=existing["confidence"],
        actor=actor_text,
    )
    if successor["status"] != ACTIVE:
        raise OrchestrationError("successor was not promoted to ACTIVE")
    old = update_memory_governance(
        conn,
        project_human_id=project,
        memory_human_id=mem_id,
        status=SUPERSEDED,
        superseded_by_memory_human_id=successor["memory_human_id"],
        rejection_code="superseded",
        rejection_reason=reason_text,
    )
    append_memory_event(
        conn,
        project_human_id=project,
        memory_human_id=mem_id,
        event_type="superseded",
        actor=actor_text,
        rejection_code="superseded",
        rejection_reason=reason_text,
    )
    return _admin_result(
        project_human_id=project,
        action="supersede",
        actor=actor_text,
        reason=reason_text,
        memory=old,
        successor=successor,
    )


def _reject(
    conn,
    *,
    project: str,
    agent_role: str,
    memory_key: str,
    title: str,
    evidence: str | None,
    source_job_human_id: str | None,
    existing: dict[str, Any] | None,
    code: str,
    reason: str,
) -> dict[str, Any]:
    memory_id = existing["memory_human_id"] if existing else f"MEM-{memory_key}"
    row = upsert_agent_memory(
        conn,
        project_human_id=project,
        memory_human_id=memory_id,
        agent_role=agent_role,
        memory_kind=SAFE_KIND if code != "not_agent_memory" else "OTHER",
        memory_key=memory_key,
        title=title,
        evidence_ref=evidence,
        source_job_human_id=source_job_human_id,
        confidence=existing["confidence"] if existing else 0.0,
        occurrence_count=existing["occurrence_count"] if existing else 1,
        last_validated_at=None,
        status=REJECTED,
        promotion_mode=AUTO_LEARNED,
        rejection_code=code,
        rejection_reason=reason,
    )
    append_memory_event(
        conn,
        project_human_id=project,
        memory_human_id=row["memory_human_id"],
        event_type="rejected",
        job_human_id=source_job_human_id,
        rejection_code=code,
        rejection_reason=reason,
    )
    return row
