"""Governed Sponsor decisions. Chat and free-text never grant approval."""

from __future__ import annotations

import uuid
from typing import Any

from projectos.errors import OrchestrationError
from projectos.store import (
    TERMINAL_STATUSES,
    append_governance_decision_event,
    find_open_governance_decision,
    get_governance_decision,
    get_job_by_human_id,
    insert_governance_decision,
    list_governance_decision_events,
    list_governance_decisions_for_project,
    mark_cancelled,
    require_safe_id,
    set_job_sponsor_authority,
    update_governance_decision,
)

ACTIONS = frozenset(
    {
        "sponsor_reserved",
        "release_approve",
        "cancel_job",
        "recover_salvage",
        "recover_reconcile",
        "governance_change",
    }
)
TARGET_KINDS = frozenset({"job", "release", "project", "none"})
_PATH_MARKERS = ("/", "\\", "..")
CHAT_NOTICE = (
    "Sponsor decisions require an explicit Approve action with confirmation. "
    "Chat, email, or free-text cannot silently grant approval."
)


def _clean_text(value: str, *, label: str, limit: int = 2000) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise OrchestrationError(f"{label} is required")
    if any(marker in text for marker in _PATH_MARKERS):
        raise OrchestrationError(f"{label} must not contain a path")
    if len(text) > limit:
        raise OrchestrationError(f"{label} is too long")
    return text


def _clean_actor(value: str, *, label: str = "actor") -> str:
    text = str(value or "").strip()
    if not text:
        raise OrchestrationError(f"{label} is required")
    if len(text) > 128 or any(marker in text for marker in _PATH_MARKERS):
        raise OrchestrationError(f"{label} must not contain a path")
    return text


def _envelope(row: dict[str, Any], events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        **row,
        "notice": CHAT_NOTICE,
        "events": events if events is not None else [],
    }


def _with_events(conn, row: dict[str, Any]) -> dict[str, Any]:
    events = list_governance_decision_events(
        conn,
        project_human_id=row["project_human_id"],
        decision_human_id=row["decision_human_id"],
    )
    return _envelope(row, events)


def list_decisions(conn, project_human_id: str, *, status: str | None = None) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    rows = list_governance_decisions_for_project(conn, project, status=status)
    return {
        "project_human_id": project,
        "notice": CHAT_NOTICE,
        "decisions": [_with_events(conn, row) for row in rows],
    }


def get_decision(conn, project_human_id: str, decision_human_id: str) -> dict[str, Any]:
    row = get_governance_decision(
        conn, project_human_id=project_human_id, decision_human_id=decision_human_id
    )
    if row is None:
        raise OrchestrationError(f"decision {decision_human_id!r} not found")
    return _with_events(conn, row)


def open_decision(
    conn,
    *,
    project_human_id: str,
    action: str,
    reason: str,
    impact: str,
    requested_by: str,
    target_kind: str = "none",
    target_human_id: str | None = None,
) -> dict[str, Any]:
    project = require_safe_id(project_human_id, label="project_human_id")
    action_key = str(action or "").strip()
    if action_key not in ACTIONS:
        raise OrchestrationError(f"action {action!r} is not a governed decision")
    kind = str(target_kind or "none").strip()
    if kind not in TARGET_KINDS:
        raise OrchestrationError(f"target_kind {target_kind!r} is not allowed")
    target = None
    if target_human_id:
        target = require_safe_id(target_human_id, label="target_human_id")
    if action in {"cancel_job", "release_approve", "recover_salvage", "recover_reconcile"}:
        if kind not in {"job", "release"} or not target:
            raise OrchestrationError(f"{action} requires a job or release target")
        _require_job(conn, project, target)
    if action == "governance_change" and kind not in {"project", "none"}:
        raise OrchestrationError("governance_change targets the project")
    existing = find_open_governance_decision(
        conn, project_human_id=project, action=action_key, target_human_id=target
    )
    if existing is not None:
        return _with_events(conn, existing)
    hid = f"DEC-{uuid.uuid4().hex[:12]}"
    row = insert_governance_decision(
        conn,
        decision_human_id=hid,
        project_human_id=project,
        action=action_key,
        target_kind=kind,
        target_human_id=target,
        reason=_clean_text(reason, label="reason"),
        impact=_clean_text(impact, label="impact"),
        requested_by=_clean_actor(requested_by, label="requested_by"),
    )
    return _with_events(conn, row)


def record_intake_decisions(
    conn,
    *,
    project_human_id: str,
    decision_requests: list[dict[str, str]],
    requested_by: str = "intake",
) -> list[dict[str, Any]]:
    opened: list[dict[str, Any]] = []
    for item in decision_requests:
        code = str(item.get("code") or "sponsor_reserved")
        question = str(item.get("question") or "Sponsor-reserved decision is required.")
        opened.append(
            open_decision(
                conn,
                project_human_id=project_human_id,
                action="sponsor_reserved",
                target_kind="none",
                target_human_id=require_safe_id(code, label="decision_code")
                if _looks_like_id(code)
                else None,
                reason=question,
                impact="Work intake is blocked until a Sponsor grants this on the Decisions page.",
                requested_by=requested_by,
            )
        )
    return opened


def approve_decision(
    conn,
    *,
    project_human_id: str,
    decision_human_id: str,
    confirmed: bool,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    if not confirmed:
        raise OrchestrationError("confirmation is required")
    actor_text = _clean_actor(actor)
    reason_text = _clean_text(reason, label="reason")
    row = get_governance_decision(
        conn, project_human_id=project_human_id, decision_human_id=decision_human_id
    )
    if row is None:
        raise OrchestrationError(f"decision {decision_human_id!r} not found")
    if row["status"] != "OPEN":
        raise OrchestrationError(
            f"decision {decision_human_id!r} is {row['status']} and cannot be approved"
        )
    result = _apply_approved_action(conn, row, actor=actor_text, reason=reason_text)
    updated = update_governance_decision(
        conn,
        project_human_id=row["project_human_id"],
        decision_human_id=row["decision_human_id"],
        status="APPROVED",
        decided_by=actor_text,
        decision_reason=reason_text,
        execution_result=result,
    )
    append_governance_decision_event(
        conn,
        project_human_id=updated["project_human_id"],
        decision_human_id=updated["decision_human_id"],
        event_type="approved",
        actor=actor_text,
        reason=reason_text,
    )
    append_governance_decision_event(
        conn,
        project_human_id=updated["project_human_id"],
        decision_human_id=updated["decision_human_id"],
        event_type="executed",
        actor=actor_text,
        reason=result,
    )
    return _with_events(conn, updated)


def reject_decision(
    conn,
    *,
    project_human_id: str,
    decision_human_id: str,
    confirmed: bool,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    if not confirmed:
        raise OrchestrationError("confirmation is required")
    actor_text = _clean_actor(actor)
    reason_text = _clean_text(reason, label="reason")
    row = get_governance_decision(
        conn, project_human_id=project_human_id, decision_human_id=decision_human_id
    )
    if row is None:
        raise OrchestrationError(f"decision {decision_human_id!r} not found")
    if row["status"] != "OPEN":
        raise OrchestrationError(
            f"decision {decision_human_id!r} is {row['status']} and cannot be rejected"
        )
    updated = update_governance_decision(
        conn,
        project_human_id=row["project_human_id"],
        decision_human_id=row["decision_human_id"],
        status="REJECTED",
        decided_by=actor_text,
        decision_reason=reason_text,
        execution_result=None,
    )
    append_governance_decision_event(
        conn,
        project_human_id=updated["project_human_id"],
        decision_human_id=updated["decision_human_id"],
        event_type="rejected",
        actor=actor_text,
        reason=reason_text,
    )
    return _with_events(conn, updated)


def _looks_like_id(value: str) -> bool:
    try:
        require_safe_id(value, label="id")
    except OrchestrationError:
        return False
    return True


def _require_job(conn, project_human_id: str, job_human_id: str):
    job = get_job_by_human_id(conn, job_human_id)
    if job is None:
        raise OrchestrationError(f"job {job_human_id!r} not found")
    if job.project_human_id != project_human_id:
        raise OrchestrationError(
            f"job {job_human_id!r} does not belong to {project_human_id!r}"
        )
    return job


def _apply_approved_action(conn, row: dict[str, Any], *, actor: str, reason: str) -> str:
    action = row["action"]
    target = row.get("target_human_id")
    project = row["project_human_id"]
    if action == "sponsor_reserved":
        return "sponsor reservation granted; intake may proceed under PM authority"
    if action == "cancel_job":
        job = _require_job(conn, project, str(target))
        if job.status in TERMINAL_STATUSES:
            raise OrchestrationError(f"job {job.human_id!r} is already {job.status}")
        mark_cancelled(conn, job.id, reason=reason)
        return f"cancelled {job.human_id}"
    if action == "release_approve":
        job = _require_job(conn, project, str(target))
        if job.queue != "RELEASE":
            raise OrchestrationError("release_approve requires a RELEASE job")
        set_job_sponsor_authority(conn, job.id, authority="approved")
        return f"sponsor release grant recorded on {job.human_id}"
    if action == "governance_change":
        from projectos.store import set_project_paused

        set_project_paused(conn, project, paused=True, reason=reason)
        return f"governance pause applied to {project}"
    if action in {"recover_salvage", "recover_reconcile"}:
        return (
            f"{action} authorized for {target}. "
            "Destructive recovery remains a separate operator command after this grant."
        )
    raise OrchestrationError(f"action {action!r} cannot be executed")
