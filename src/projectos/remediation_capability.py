"""Finding owner vs execution capability — assessors do not remediate source."""

from __future__ import annotations

from typing import Any

from projectos.domain_events import (
    ACTOR_ARCHITECTURE,
    ACTOR_DELIVERY,
    ACTOR_DEVELOPER,
    ACTOR_RELEASE,
    ACTOR_SECURITY,
)
from projectos.finding_routing import (
    ARCHITECTURE_VIOLATION,
    DELIVERY_CONFIGURATION,
    DOCUMENTATION_GOVERNANCE,
    PACKAGING_DEFECT,
    RELEASE_CONFIGURATION,
    SECURITY_FINDING,
    SOURCE_CODE_DEFECT,
    TEST_INFRASTRUCTURE,
    UNKNOWN,
    route_finding_to_agent,
)

# (finding_owner_agent, execution_queue)
_EXECUTION_CAPABILITY: dict[str, tuple[str, str]] = {
    SOURCE_CODE_DEFECT: (ACTOR_DEVELOPER, "DELIVERY"),
    ARCHITECTURE_VIOLATION: (ACTOR_DEVELOPER, "DELIVERY"),
    SECURITY_FINDING: (ACTOR_DEVELOPER, "DELIVERY"),
    PACKAGING_DEFECT: (ACTOR_DELIVERY, "DELIVERY"),
    DELIVERY_CONFIGURATION: (ACTOR_DELIVERY, "DELIVERY"),
    TEST_INFRASTRUCTURE: (ACTOR_DEVELOPER, "DELIVERY"),
    RELEASE_CONFIGURATION: (ACTOR_RELEASE, "DELIVERY"),
    DOCUMENTATION_GOVERNANCE: (ACTOR_DEVELOPER, "DELIVERY"),
    UNKNOWN: (ACTOR_DEVELOPER, "DELIVERY"),
}


def resolve_remediation_execution(finding: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return (finding_owner, assigned_agent, execution_queue, reason)."""
    owner, reason = route_finding_to_agent(finding)
    category = str(finding.get("category") or UNKNOWN)
    capability = _EXECUTION_CAPABILITY.get(category, (ACTOR_DEVELOPER, "DELIVERY"))
    executor_agent, execution_queue = capability
    # Security/architecture owners advise; code-modifying worker executes source fixes.
    if category in {SECURITY_FINDING, ARCHITECTURE_VIOLATION}:
        assigned_agent = executor_agent
        exec_reason = (
            f"{owner} owns finding; {executor_agent} executes corrective source work"
        )
    else:
        assigned_agent = owner if owner in {ACTOR_DELIVERY, ACTOR_RELEASE} else executor_agent
        exec_reason = reason
    return owner, assigned_agent, execution_queue, exec_reason


def group_findings_for_remediation(
    findings: list[dict[str, Any]],
) -> list[tuple[str, str, list[dict[str, Any]], str]]:
    """Group findings by compatible execution capability."""
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    reasons: dict[tuple[str, str], str] = {}
    for finding in findings:
        owner, assigned, queue, reason = resolve_remediation_execution(finding)
        key = (assigned, queue)
        buckets.setdefault(key, []).append({**finding, "finding_owner": owner})
        reasons.setdefault(key, reason)
    return [
        (assigned, queue, items, reasons[(assigned, queue)])
        for (assigned, queue), items in buckets.items()
    ]
