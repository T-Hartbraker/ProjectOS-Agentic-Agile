"""Tests for canonical ExecutionRun outcome taxonomy."""

from __future__ import annotations

import pytest

from projectos.run_outcomes import (
    EVENT_RUN_BLOCKED,
    EVENT_RUN_CANCELLED,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_ESCALATED,
    EVENT_WAITING_FOR_SPONSOR,
    OUTCOME_CANCELLED_BY_SPONSOR,
    OUTCOME_MAX_REMEDIATION_EXCEEDED,
    OUTCOME_SPONSOR_DECISION_REQUIRED,
    OUTCOME_SUCCESS,
    OUTCOME_UNRECOVERABLE_TECHNICAL,
    event_for_outcome,
    is_terminal_outcome,
    is_terminal_run_event,
    resolve_outcome,
    run_status_for_outcome,
)


@pytest.mark.parametrize(
    "outcome,event,status,terminal",
    [
        (OUTCOME_SUCCESS, EVENT_RUN_COMPLETED, "COMPLETED", True),
        (OUTCOME_SPONSOR_DECISION_REQUIRED, EVENT_WAITING_FOR_SPONSOR, "WAITING_FOR_SPONSOR", False),
        (OUTCOME_UNRECOVERABLE_TECHNICAL, EVENT_RUN_BLOCKED, "BLOCKED", True),
        (OUTCOME_MAX_REMEDIATION_EXCEEDED, EVENT_RUN_ESCALATED, "ESCALATED", True),
        (OUTCOME_CANCELLED_BY_SPONSOR, EVENT_RUN_CANCELLED, "CANCELLED", True),
    ],
)
def test_outcome_mappings(outcome, event, status, terminal) -> None:
    assert event_for_outcome(outcome) == event
    assert run_status_for_outcome(outcome) == status
    assert is_terminal_outcome(outcome) is terminal
    assert is_terminal_run_event(event) is terminal or event == EVENT_WAITING_FOR_SPONSOR


def test_legacy_terminal_status_resolves() -> None:
    assert resolve_outcome("BLOCKED") == OUTCOME_UNRECOVERABLE_TECHNICAL
    assert resolve_outcome("CANCELLED") == OUTCOME_CANCELLED_BY_SPONSOR
