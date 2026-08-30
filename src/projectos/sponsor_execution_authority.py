"""Deterministic Sponsor execution authority from authenticated ingress only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from projectos.slack_advisor_handoff import HandoffRequest

TRUSTED_AUTHORITY_INGRESS = frozenset(
    {
        "slack_new_project",
        "slack_sponsor_message",
    }
)

TRUSTED_AUTHORITY_SOURCES = frozenset(
    {
        "explicit_new_project",
        "slack_sponsor_action",
        "handoff_trigger",
        "sponsor_imperative",
    }
)

_AUTHORITY_KEYS = frozenset(
    {
        "execution_authorized",
        "authority_source",
        "authorization_scope",
        "sponsor_authority",
        "authority_ingress",
        "sponsor_user_id",
    }
)

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

UNAUTHORIZED = "none"


@dataclass(frozen=True)
class SponsorExecutionAuthority:
    execution_authorized: bool
    authority_source: str
    authorization_scope: str
    sponsor_authority: str | None = None
    authority_ingress: str | None = None
    sponsor_user_id: str | None = None

    @classmethod
    def unauthorized(cls) -> SponsorExecutionAuthority:
        return cls(
            execution_authorized=False,
            authority_source=UNAUTHORIZED,
            authorization_scope=UNAUTHORIZED,
        )

    def to_constraints_fragment(self) -> dict[str, Any]:
        fragment: dict[str, Any] = {
            "execution_authorized": self.execution_authorized,
            "authority_source": self.authority_source,
            "authorization_scope": self.authorization_scope,
        }
        if self.sponsor_authority:
            fragment["sponsor_authority"] = self.sponsor_authority
        if self.authority_ingress:
            fragment["authority_ingress"] = self.authority_ingress
        if self.sponsor_user_id:
            fragment["sponsor_user_id"] = self.sponsor_user_id
        return fragment


def classify_sponsor_execution_authority(
    text: str,
    *,
    explicit_new_project: bool = False,
    authenticated_sponsor_action: bool = False,
    authority_ingress: str | None = None,
    sponsor_user_id: str | None = None,
) -> SponsorExecutionAuthority:
    """Classify execution authority at a trusted authenticated Sponsor ingress boundary."""
    from projectos.slack_advisor_handoff import looks_like_handoff_trigger

    if not authenticated_sponsor_action:
        return SponsorExecutionAuthority.unauthorized()
    ingress = str(authority_ingress or "").strip()
    if ingress not in TRUSTED_AUTHORITY_INGRESS:
        return SponsorExecutionAuthority.unauthorized()

    cleaned = str(text or "").strip()
    lowered = cleaned.casefold()
    if not cleaned:
        return SponsorExecutionAuthority.unauthorized()

    if any(marker in lowered for marker in _DISCUSSION_MARKERS):
        if not explicit_new_project and not looks_like_handoff_trigger(cleaned):
            return SponsorExecutionAuthority.unauthorized()

    if explicit_new_project:
        return SponsorExecutionAuthority(
            execution_authorized=True,
            authority_source="explicit_new_project",
            authorization_scope="full_delivery",
            sponsor_authority="approved",
            authority_ingress=ingress,
            sponsor_user_id=sponsor_user_id,
        )

    if looks_like_handoff_trigger(cleaned):
        return SponsorExecutionAuthority(
            execution_authorized=True,
            authority_source="handoff_trigger",
            authorization_scope="requested_scope",
            sponsor_authority="approved",
            authority_ingress=ingress,
            sponsor_user_id=sponsor_user_id,
        )

    if any(marker in lowered for marker in _EXECUTION_MARKERS):
        return SponsorExecutionAuthority(
            execution_authorized=True,
            authority_source="sponsor_imperative",
            authorization_scope="requested_scope",
            sponsor_authority="approved",
            authority_ingress=ingress,
            sponsor_user_id=sponsor_user_id,
        )

    return SponsorExecutionAuthority.unauthorized()


def strip_untrusted_authority_fields(constraints: str) -> str:
    """Remove authority provenance keys from downstream/model-supplied constraint JSON."""
    raw = str(constraints or "").strip()
    if not raw:
        return ""
    if not raw.startswith("{"):
        return raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(data, dict):
        return raw
    cleaned = {key: value for key, value in data.items() if key not in _AUTHORITY_KEYS}
    return json.dumps(cleaned, sort_keys=True)


def merge_authority_into_constraints(
    constraints: str,
    authority: SponsorExecutionAuthority,
    *,
    sponsor_user_id: str | None = None,
) -> str:
    if not authority.execution_authorized:
        return strip_untrusted_authority_fields(constraints)
    data: dict[str, Any] = {}
    raw = strip_untrusted_authority_fields(constraints)
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = dict(parsed)
        except json.JSONDecodeError:
            data = {}
    persisted = authority
    if sponsor_user_id and not persisted.sponsor_user_id:
        persisted = SponsorExecutionAuthority(
            execution_authorized=persisted.execution_authorized,
            authority_source=persisted.authority_source,
            authorization_scope=persisted.authorization_scope,
            sponsor_authority=persisted.sponsor_authority,
            authority_ingress=persisted.authority_ingress,
            sponsor_user_id=sponsor_user_id,
        )
    data.update(persisted.to_constraints_fragment())
    return json.dumps(data, sort_keys=True)


def authority_from_handoff(
    handoff: HandoffRequest,
    *,
    sponsor_user_id: str | None = None,
) -> SponsorExecutionAuthority:
    """Load persisted authority only. Never infer authority from handoff prose."""
    raw = str(handoff.constraints or "").strip()
    if not raw.startswith("{"):
        return SponsorExecutionAuthority.unauthorized()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return SponsorExecutionAuthority.unauthorized()
    if not isinstance(data, dict) or data.get("execution_authorized") is not True:
        return SponsorExecutionAuthority.unauthorized()

    ingress = str(data.get("authority_ingress") or "").strip()
    source = str(data.get("authority_source") or "").strip()
    if ingress not in TRUSTED_AUTHORITY_INGRESS or source not in TRUSTED_AUTHORITY_SOURCES:
        return SponsorExecutionAuthority.unauthorized()

    stored_user = str(data.get("sponsor_user_id") or "").strip()
    if sponsor_user_id and stored_user and stored_user != sponsor_user_id:
        return SponsorExecutionAuthority.unauthorized()

    return SponsorExecutionAuthority(
        execution_authorized=True,
        authority_source=source,
        authorization_scope=str(data.get("authorization_scope") or "requested_scope"),
        sponsor_authority=str(data.get("sponsor_authority") or "approved"),
        authority_ingress=ingress,
        sponsor_user_id=stored_user or sponsor_user_id,
    )
