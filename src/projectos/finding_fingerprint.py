"""Deterministic finding fingerprints for recurrence tracking."""

from __future__ import annotations

import hashlib
import re
from typing import Any

_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", str(text or "").strip().lower())


def finding_fingerprint(finding: dict[str, Any]) -> str:
    """Stable defect signature across candidates."""
    parts = [
        _normalize(str(finding.get("category") or "")),
        _normalize(str(finding.get("affected_component") or "")),
        _normalize(str(finding.get("expected_condition") or "")),
        _normalize(str(finding.get("actual_condition") or "")),
        _normalize(str(finding.get("source_gate_or_review") or finding.get("assurance_role") or "")),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"FP-{digest.upper()}"


def durable_finding_id(finding: dict[str, Any], *, qa_evidence_id: int | None = None) -> str:
    """Derive durable finding ID from fingerprint and evidence row when available."""
    fp = finding_fingerprint(finding)
    if qa_evidence_id is not None:
        return f"FND-{qa_evidence_id}-{fp[3:]}"
    return f"FND-{fp[3:]}"
