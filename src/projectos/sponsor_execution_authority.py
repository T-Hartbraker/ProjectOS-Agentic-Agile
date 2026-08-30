"""Deterministic Sponsor execution authority from authenticated action requests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from projectos.slack_advisor_handoff import HandoffRequest, looks_like_handoff_trigger

_DISCUSSION_MARKERS = (
    "what should we",
    "what do you think",
    "give me ideas",
    "some ideas",
    "recommend",
    "recommendation",
    "thoughts on",
    "help me understand",
    "brainstorm",
    "assess this project",
    "assess the project",
    "what are our options",
    "options for",
)

_EXECUTION_MARKERS = (
    "proceed autonomously",
    "proceed with",
    "go ahead",
    "full projectos delivery process",
    "let's execute",
    "lets execute",
    "submit this work",
    "send that to projectos",
    "send this to projectos",
    "have projectos",
)


@dataclass(frozen=True)
class SponsorExecutionAuthority:
    execution_authorized: bool
    authority_source: str
    authorization_scope: str
    sponsor_authority: str | None = None

    def to_constraints_fragment(self) -> dict[str, Any]:
        return {
            "execution_authorized": self.execution_authorized,
            "authority_source": self.authority_source,
            "authorization_scope": self.authorization_scope,
            "sponsor_authority": self.sponsor_authority,
        }


def classify_sponsor_execution_authority(
    text: str,
    *,
    explicit_new_project: bool = False,
    authenticated_sponsor_action: bool = True,
) -> SponsorExecutionAuthority:
    """Classify whether an authenticated Sponsor action already authorizes execution."""
    if not authenticated_sponsor_action:
        return SponsorExecutionAuthority(
            execution_authorized=False,
            authority_source="none",
            authorization_scope="none",
        )

    cleaned = str(text or "").strip()
    lowered = cleaned.casefold()
    if not cleaned:
        return SponsorExecutionAuthority(
            execution_authorized=False,
            authority_source="none",
            authorization_scope="none",
        )

    if any(marker in lowered for marker in _DISCUSSION_MARKERS):
        if not explicit_new_project and not looks_like_handoff_trigger(cleaned):
            return SponsorExecutionAuthority(
                execution_authorized=False,
                authority_source="none",
                authorization_scope="none",
            )

    if explicit_new_project:
        return SponsorExecutionAuthority(
            execution_authorized=True,
            authority_source="explicit_new_project",
            authorization_scope="full_delivery",
            sponsor_authority="approved",
        )

    if looks_like_handoff_trigger(cleaned):
        return SponsorExecutionAuthority(
            execution_authorized=True,
            authority_source="handoff_trigger",
            authorization_scope="requested_scope",
            sponsor_authority="approved",
        )

    if any(marker in lowered for marker in _EXECUTION_MARKERS):
        return SponsorExecutionAuthority(
            execution_authorized=True,
            authority_source="sponsor_imperative",
            authorization_scope="requested_scope",
            sponsor_authority="approved",
        )

    return SponsorExecutionAuthority(
        execution_authorized=False,
        authority_source="none",
        authorization_scope="none",
    )


def merge_authority_into_constraints(
    constraints: str,
    authority: SponsorExecutionAuthority,
) -> str:
    data: dict[str, Any] = {}
    raw = str(constraints or "").strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = dict(parsed)
        except json.JSONDecodeError:
            data = {}
    data.update(authority.to_constraints_fragment())
    return json.dumps(data, sort_keys=True)


def authority_from_handoff(
    handoff: HandoffRequest,
    *,
    explicit_new_project: bool = False,
) -> SponsorExecutionAuthority:
    raw = str(handoff.constraints or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("execution_authorized") is True:
                return SponsorExecutionAuthority(
                    execution_authorized=True,
                    authority_source=str(data.get("authority_source") or "stored"),
                    authorization_scope=str(
                        data.get("authorization_scope") or "requested_scope"
                    ),
                    sponsor_authority=str(data.get("sponsor_authority") or "approved"),
                )
        except json.JSONDecodeError:
            pass
    return classify_sponsor_execution_authority(
        handoff.objective,
        explicit_new_project=explicit_new_project,
    )
