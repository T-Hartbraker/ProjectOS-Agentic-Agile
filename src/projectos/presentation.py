"""Operator-facing labels for queues, roles, and statuses.

Canonical IDs stay authoritative. This module only maps them for display.
"""

from __future__ import annotations

STATUS_LABELS = {
    "QUEUED": "Queued",
    "READY": "Ready",
    "LEASED": "Assigned",
    "RUNNING": "In progress",
    "SUCCEEDED": "Finished",
    "FAILED": "Failed",
    "BLOCKED": "Blocked",
    "RETRY_WAIT": "Waiting to retry",
    "CANCELLED": "Cancelled",
}

QUEUE_LABELS = {
    "DELIVERY": "Delivery",
    "INTEGRATION": "Integration",
    "RELEASE": "Release",
    "PM": "Planning",
    "ASSURANCE_FUNCTIONAL": "Functional review",
    "ASSURANCE_QUALITY": "Quality review",
    "ASSURANCE_SECURITY": "Security review",
    "ASSURANCE_INTEGRATION": "Integration review",
}

ROLE_LABELS = {
    "DELIVERY": "Delivery agent",
    "INTEGRATION": "Integration agent",
    "RELEASE": "Release agent",
    "PM": "Planner",
    "ASSURANCE_FUNCTIONAL": "Functional reviewer",
    "ASSURANCE_QUALITY": "Quality reviewer",
    "ASSURANCE_SECURITY": "Security reviewer",
    "ASSURANCE_INTEGRATION": "Integration reviewer",
}

LANE_LABELS = {
    "delivery": "Delivery",
    "assurance": "Quality",
    "control": "Control",
}

HEALTH_LABELS = {
    "healthy": "Healthy",
    "degraded": "Needs attention",
    "paused": "Paused",
    "blocked": "Blocked",
    "disabled": "Disabled",
}


def humanize_token(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "Not reported"
    words = text.replace("_", " ").replace("-", " ").split()
    return " ".join(word[:1].upper() + word[1:].lower() for word in words if word)


def status_label(status: str | None) -> str:
    key = str(status or "").strip()
    return STATUS_LABELS.get(key, humanize_token(key))


def queue_label(queue: str | None) -> str:
    key = str(queue or "").strip()
    return QUEUE_LABELS.get(key, humanize_token(key))


def role_label(role: str | None) -> str:
    key = str(role or "").strip()
    return ROLE_LABELS.get(key, humanize_token(key))


def lane_label(lane: str | None) -> str:
    key = str(lane or "").strip()
    return LANE_LABELS.get(key, humanize_token(key))


def health_label(status: str | None) -> str:
    key = str(status or "").strip().lower()
    return HEALTH_LABELS.get(key, humanize_token(key))


def activity_sentence(*, queue: str | None, work_item_human_id: str | None, status: str | None) -> str:
    activity = queue_label(queue)
    key = str(queue or "").strip()
    if str(status or "") == "RUNNING":
        if key == "INTEGRATION":
            if work_item_human_id:
                return (
                    f"Combining the approved implementation for {work_item_human_id} "
                    "into the iteration candidate."
                )
            return "Combining the approved implementation into the iteration candidate."
        if work_item_human_id:
            return f"{activity} is in progress for {work_item_human_id}."
        return f"{activity} is in progress."
    if str(status or "") == "BLOCKED":
        return f"{activity} is blocked."
    if str(status or "") == "SUCCEEDED":
        return f"{activity} finished."
    return f"{activity} is {status_label(status).lower()}."


def next_step_sentence(queue: str | None) -> str:
    key = str(queue or "").strip()
    if key == "DELIVERY":
        return "Independent reviews begin after delivery succeeds."
    if key.startswith("ASSURANCE"):
        return "Release verification begins after independent reviews succeed."
    if key == "INTEGRATION":
        return "Release verification begins after integration succeeds."
    if key == "RELEASE":
        return "The iteration is complete if release succeeds."
    if key == "PM":
        return "Delivery work is created after planning is accepted."
    return "The next governed job starts when this one succeeds."
