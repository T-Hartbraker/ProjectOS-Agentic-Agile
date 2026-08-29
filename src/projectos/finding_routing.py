"""Deterministic QA finding classification and PM agent routing."""

from __future__ import annotations

import re
from typing import Any

from projectos.domain_events import (
    ACTOR_ARCHITECTURE,
    ACTOR_DELIVERY,
    ACTOR_DEVELOPER,
    ACTOR_PM,
    ACTOR_QA,
    ACTOR_RELEASE,
    ACTOR_SECURITY,
)

SOURCE_CODE_DEFECT = "SOURCE_CODE_DEFECT"
ARCHITECTURE_VIOLATION = "ARCHITECTURE_VIOLATION"
SECURITY_FINDING = "SECURITY_FINDING"
PACKAGING_DEFECT = "PACKAGING_DEFECT"
DELIVERY_CONFIGURATION = "DELIVERY_CONFIGURATION"
TEST_INFRASTRUCTURE = "TEST_INFRASTRUCTURE"
RELEASE_CONFIGURATION = "RELEASE_CONFIGURATION"
DOCUMENTATION_GOVERNANCE = "DOCUMENTATION_GOVERNANCE"
UNKNOWN = "UNKNOWN"

_CATEGORY_TO_AGENT = {
    SOURCE_CODE_DEFECT: ACTOR_DEVELOPER,
    ARCHITECTURE_VIOLATION: ACTOR_ARCHITECTURE,
    SECURITY_FINDING: ACTOR_SECURITY,
    PACKAGING_DEFECT: ACTOR_DELIVERY,
    DELIVERY_CONFIGURATION: ACTOR_DELIVERY,
    TEST_INFRASTRUCTURE: ACTOR_DEVELOPER,
    RELEASE_CONFIGURATION: ACTOR_RELEASE,
    DOCUMENTATION_GOVERNANCE: ACTOR_PM,
    UNKNOWN: ACTOR_PM,
}

_ASSURANCE_ROLE_CATEGORY = {
    "ASSURANCE_FUNCTIONAL": SOURCE_CODE_DEFECT,
    "ASSURANCE_INTEGRATION": SOURCE_CODE_DEFECT,
    "ASSURANCE_SECURITY": SECURITY_FINDING,
    "ASSURANCE_QUALITY": TEST_INFRASTRUCTURE,
}

_LEGACY_PREFIX_RE = re.compile(r"^ASSURANCE_\d+$")


def classify_finding_category(*, assurance_role: str, actual_condition: str = "") -> str:
    role = str(assurance_role or "").upper()
    if role in _ASSURANCE_ROLE_CATEGORY:
        return _ASSURANCE_ROLE_CATEGORY[role]
    lowered = str(actual_condition or "").lower()
    if "security" in lowered or "vuln" in lowered:
        return SECURITY_FINDING
    if "architecture" in lowered or "design" in lowered:
        return ARCHITECTURE_VIOLATION
    if "package" in lowered or "installer" in lowered or "artifact" in lowered:
        return PACKAGING_DEFECT
    if "delivery.json" in lowered or "configuration" in lowered:
        return DELIVERY_CONFIGURATION
    if "release" in lowered:
        return RELEASE_CONFIGURATION
    if "documentation" in lowered or "governance" in lowered:
        return DOCUMENTATION_GOVERNANCE
    if _LEGACY_PREFIX_RE.match(role):
        return SOURCE_CODE_DEFECT
    return UNKNOWN


def route_finding_to_agent(finding: dict[str, Any]) -> tuple[str, str]:
    """Return (assigned_agent, reason). PM remains authoritative; QA recommendation is advisory."""
    category = str(
        finding.get("category")
        or classify_finding_category(
            assurance_role=str(finding.get("source_gate_or_review") or finding.get("assurance_role") or ""),
            actual_condition=str(finding.get("actual_condition") or ""),
        )
    )
    recommended = str(finding.get("recommended_owner_role") or "").lower()
    if recommended in {"developer", "delivery", "architecture", "security", "qa", "release"}:
        mapping = {
            "developer": ACTOR_DEVELOPER,
            "delivery": ACTOR_DELIVERY,
            "architecture": ACTOR_ARCHITECTURE,
            "security": ACTOR_SECURITY,
            "qa": ACTOR_QA,
            "release": ACTOR_RELEASE,
        }
        agent = mapping.get(recommended, _CATEGORY_TO_AGENT.get(category, ACTOR_PM))
        return agent, f"QA recommended {recommended}; PM routed by category {category}"
    agent = _CATEGORY_TO_AGENT.get(category, ACTOR_PM)
    if category == UNKNOWN:
        return ACTOR_PM, "PM evaluating finding with unknown category"
    return agent, f"PM routed {category} to {agent}"
