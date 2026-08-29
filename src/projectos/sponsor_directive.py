"""Classify Sponsor ingress as new objective vs active-run directive."""

from __future__ import annotations

import re
from typing import Literal

from projectos.execution_run import ExecutionRunRecord
from projectos.run_outcomes import ACTIVE_RUN_STATUSES, is_terminal_run_status
from projectos.slack_advisor_handoff import HandoffRequest
from projectos.sponsor_handoff import SponsorHandoffRecord

DirectiveKind = Literal["NEW_OBJECTIVE", "ACTIVE_RUN_DIRECTIVE"]

_FOLLOW_UP_MARKERS = re.compile(
    r"\b(investigate|resolve|fix|continue|retry|update|also|instead|change|amend|"
    r"prioritize|blocker|error|issue|context|correct|remediate|unblock|proceed)\b",
    re.IGNORECASE,
)
_SEPARATE_OBJECTIVE_MARKERS = re.compile(
    r"\b(new objective|separate request|different project|start over|unrelated|brand new)\b",
    re.IGNORECASE,
)


def _token_set(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]{3,}", text.lower())}


def classify_sponsor_ingress(
    *,
    handoff: HandoffRequest,
    existing_handoff: SponsorHandoffRecord | None,
    active_run: ExecutionRunRecord | None,
    request_type: str,
) -> DirectiveKind:
    """Return NEW_OBJECTIVE or ACTIVE_RUN_DIRECTIVE."""
    if active_run is None or is_terminal_run_status(active_run.status):
        return "NEW_OBJECTIVE"
    if active_run.status not in ACTIVE_RUN_STATUSES and active_run.status != "RUNNING":
        return "NEW_OBJECTIVE"
    if existing_handoff and existing_handoff.project_id != handoff.project_id:
        return "NEW_OBJECTIVE"
    if _SEPARATE_OBJECTIVE_MARKERS.search(handoff.objective):
        return "NEW_OBJECTIVE"

    new_tokens = _token_set(handoff.objective)
    run_tokens = _token_set(active_run.objective)
    overlap = new_tokens & run_tokens

    if _FOLLOW_UP_MARKERS.search(handoff.objective):
        return "ACTIVE_RUN_DIRECTIVE"
    if overlap:
        return "ACTIVE_RUN_DIRECTIVE"
    if request_type == active_run.request_type:
        return "ACTIVE_RUN_DIRECTIVE"

    return "NEW_OBJECTIVE"


def directive_requires_replan(handoff: HandoffRequest, *, request_type: str) -> bool:
    lowered = handoff.objective.lower()
    if request_type in {"DEFECT", "WORK", "QUALITY"}:
        return True
    return bool(
        _FOLLOW_UP_MARKERS.search(handoff.objective)
        or "investigate" in lowered
        or "fix" in lowered
        or "resolve" in lowered
    )
