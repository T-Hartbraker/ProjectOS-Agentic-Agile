"""Release gate definitions and evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RELEASE_GATES = (
    "QA_GATE",
    "SOURCE_GATE",
    "BUILD_GATE",
    "PACKAGE_GATE",
    "CHECKSUM_GATE",
    "SBOM_GATE",
    "SIGNATURE_GATE",
    "PUBLICATION_GATE",
    "DELIVERY_GATE",
)

GATE_STATUS_PENDING = "pending"
GATE_STATUS_PASSED = "passed"
GATE_STATUS_FAILED = "failed"
GATE_STATUS_SKIPPED = "skipped"
GATE_STATUS_NOT_REQUIRED = "not_required"


@dataclass(frozen=True)
class GateResult:
    gate_name: str
    status: str
    detail: str = ""
    evidence: dict[str, Any] | None = None


def initial_gate_statuses(*, signing_required: bool, sbom_required: bool, github_enabled: bool) -> dict[str, str]:
    statuses = {gate: GATE_STATUS_PENDING for gate in RELEASE_GATES}
    if not signing_required:
        statuses["SIGNATURE_GATE"] = GATE_STATUS_NOT_REQUIRED
    if not sbom_required:
        statuses["SBOM_GATE"] = GATE_STATUS_NOT_REQUIRED
    if not github_enabled:
        statuses["PUBLICATION_GATE"] = GATE_STATUS_SKIPPED
    return statuses


def all_required_gates_passed(gates: dict[str, str]) -> bool:
    for gate in RELEASE_GATES:
        status = gates.get(gate, GATE_STATUS_PENDING)
        if status in {GATE_STATUS_FAILED, GATE_STATUS_PENDING}:
            return False
    return True


def blocking_gates(gates: dict[str, str]) -> list[str]:
    return [
        gate
        for gate in RELEASE_GATES
        if gates.get(gate, GATE_STATUS_PENDING) in {GATE_STATUS_FAILED, GATE_STATUS_PENDING}
    ]
