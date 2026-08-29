"""Canonical ExecutionRun outcome taxonomy — business semantics to events and status."""

from __future__ import annotations

# Business outcome categories
OUTCOME_SUCCESS = "SUCCESS"
OUTCOME_SPONSOR_DECISION_REQUIRED = "SPONSOR_DECISION_REQUIRED"
OUTCOME_UNRECOVERABLE_TECHNICAL = "UNRECOVERABLE_TECHNICAL_CONDITION"
OUTCOME_MAX_REMEDIATION_EXCEEDED = "MAX_REMEDIATION_POLICY_EXCEEDED"
OUTCOME_CANCELLED_BY_SPONSOR = "CANCELLED_BY_SPONSOR"

# Authoritative domain events
EVENT_RUN_COMPLETED = "RUN_COMPLETED"
EVENT_WAITING_FOR_SPONSOR = "WAITING_FOR_SPONSOR"
EVENT_RUN_BLOCKED = "RUN_BLOCKED"
EVENT_RUN_ESCALATED = "RUN_ESCALATED"
EVENT_RUN_CANCELLED = "RUN_CANCELLED"

# execution_runs.status values
STATUS_COMPLETED = "COMPLETED"
STATUS_WAITING_FOR_SPONSOR = "WAITING_FOR_SPONSOR"
STATUS_BLOCKED = "BLOCKED"
STATUS_ESCALATED = "ESCALATED"
STATUS_CANCELLED = "CANCELLED"

# Legacy alias retained for existing rows and queries
STATUS_WAITING_APPROVAL = "WAITING_APPROVAL"

OUTCOME_TO_EVENT: dict[str, str] = {
    OUTCOME_SUCCESS: EVENT_RUN_COMPLETED,
    OUTCOME_SPONSOR_DECISION_REQUIRED: EVENT_WAITING_FOR_SPONSOR,
    OUTCOME_UNRECOVERABLE_TECHNICAL: EVENT_RUN_BLOCKED,
    OUTCOME_MAX_REMEDIATION_EXCEEDED: EVENT_RUN_ESCALATED,
    OUTCOME_CANCELLED_BY_SPONSOR: EVENT_RUN_CANCELLED,
}

OUTCOME_TO_RUN_STATUS: dict[str, str] = {
    OUTCOME_SUCCESS: STATUS_COMPLETED,
    OUTCOME_SPONSOR_DECISION_REQUIRED: STATUS_WAITING_FOR_SPONSOR,
    OUTCOME_UNRECOVERABLE_TECHNICAL: STATUS_BLOCKED,
    OUTCOME_MAX_REMEDIATION_EXCEEDED: STATUS_ESCALATED,
    OUTCOME_CANCELLED_BY_SPONSOR: STATUS_CANCELLED,
}

TERMINAL_OUTCOMES = frozenset(
    {
        OUTCOME_SUCCESS,
        OUTCOME_UNRECOVERABLE_TECHNICAL,
        OUTCOME_MAX_REMEDIATION_EXCEEDED,
        OUTCOME_CANCELLED_BY_SPONSOR,
    }
)

TERMINAL_RUN_EVENTS = frozenset(
    {
        EVENT_RUN_COMPLETED,
        EVENT_RUN_BLOCKED,
        EVENT_RUN_ESCALATED,
        EVENT_RUN_CANCELLED,
    }
)

TERMINAL_RUN_STATUSES = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_BLOCKED,
        STATUS_ESCALATED,
        STATUS_CANCELLED,
        "FAILED",  # legacy
    }
)

ACTIVE_RUN_STATUSES = frozenset(
    {
        "PLANNING",
        STATUS_WAITING_FOR_SPONSOR,
        STATUS_WAITING_APPROVAL,
        "RUNNING",
    }
)

_LEGACY_TERMINAL_STATUS_TO_OUTCOME: dict[str, str] = {
    "COMPLETED": OUTCOME_SUCCESS,
    "BLOCKED": OUTCOME_UNRECOVERABLE_TECHNICAL,
    "FAILED": OUTCOME_UNRECOVERABLE_TECHNICAL,
    "CANCELLED": OUTCOME_CANCELLED_BY_SPONSOR,
    "ESCALATED": OUTCOME_MAX_REMEDIATION_EXCEEDED,
}


def resolve_outcome(outcome: str) -> str:
    key = str(outcome or "").strip().upper()
    if key in OUTCOME_TO_EVENT:
        return key
    if key in _LEGACY_TERMINAL_STATUS_TO_OUTCOME:
        return _LEGACY_TERMINAL_STATUS_TO_OUTCOME[key]
    raise ValueError(f"Unknown run outcome: {outcome!r}")


def event_for_outcome(outcome: str) -> str:
    return OUTCOME_TO_EVENT[resolve_outcome(outcome)]


def run_status_for_outcome(outcome: str) -> str:
    return OUTCOME_TO_RUN_STATUS[resolve_outcome(outcome)]


def is_terminal_outcome(outcome: str) -> bool:
    return resolve_outcome(outcome) in TERMINAL_OUTCOMES


def is_terminal_run_event(event_type: str) -> bool:
    return str(event_type or "") in TERMINAL_RUN_EVENTS


def is_terminal_run_status(status: str) -> bool:
    return str(status or "") in TERMINAL_RUN_STATUSES


def normalize_waiting_status(status: str) -> str:
    """Treat legacy WAITING_APPROVAL as WAITING_FOR_SPONSOR."""
    if status == STATUS_WAITING_APPROVAL:
        return STATUS_WAITING_FOR_SPONSOR
    return status
