"""Map natural-language Sponsor outcomes to capability contracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

REQUEST_TYPES = frozenset(
    {
        "WORK",
        "PROJECT",
        "ITERATION",
        "RELEASE",
        "DELIVERY",
        "QUALITY",
        "DEFECT",
        "IMPROVEMENT",
        "GOVERNANCE",
    }
)

_RELEASE_MARKERS = re.compile(
    r"\b(re-?release|release|package|installer|publish|download link|github release)\b",
    re.IGNORECASE,
)
_DELIVERY_MARKERS = re.compile(r"\b(deliver|deployment|artifact|build)\b", re.IGNORECASE)
_QUALITY_MARKERS = re.compile(r"\b(qa|quality|test suite|validation)\b", re.IGNORECASE)
_DEFECT_MARKERS = re.compile(r"\b(defect|bug|fix)\b", re.IGNORECASE)


@dataclass(frozen=True)
class CapabilityContract:
    request_type: str
    objective: str
    desired_outputs: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    action_type: str = "work_request"

    def desired_outputs_json(self) -> str:
        return json.dumps(self.desired_outputs, sort_keys=True)

    def constraints_json(self) -> str:
        return json.dumps(self.constraints, sort_keys=True)


def classify_request(*, text: str, fallback_objective: str = "") -> CapabilityContract:
    raw = str(text or "").strip()
    objective = fallback_objective.strip() or raw
    lowered = raw.lower()

    if _RELEASE_MARKERS.search(raw):
        desired = {
            "package": True,
            "installer": "installer" in lowered,
            "publish": any(w in lowered for w in ("publish", "release", "link", "download")),
            "download_link": any(w in lowered for w in ("link", "download", "url")),
        }
        constraints = {
            "source_mutation": False,
            "use_approved_source": True,
            "use_delivery_pipeline": True,
        }
        return CapabilityContract(
            request_type="RELEASE",
            objective=objective,
            desired_outputs=desired,
            constraints=constraints,
            action_type="prepare_release",
        )

    if _DELIVERY_MARKERS.search(raw):
        return CapabilityContract(
            request_type="DELIVERY",
            objective=objective,
            desired_outputs={"artifacts": True},
            constraints={"use_delivery_pipeline": True},
            action_type="package_release",
        )

    if _QUALITY_MARKERS.search(raw):
        return CapabilityContract(
            request_type="QUALITY",
            objective=objective,
            desired_outputs={"quality_report": True},
            action_type="work_request",
        )

    if _DEFECT_MARKERS.search(raw):
        return CapabilityContract(
            request_type="DEFECT",
            objective=objective,
            action_type="work_request",
        )

    return CapabilityContract(
        request_type="WORK",
        objective=objective,
        action_type="work_request",
    )
