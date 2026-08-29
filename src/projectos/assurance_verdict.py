"""Structured assurance verdict contract — execution success is not QA verdict."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from projectos.errors import OrchestrationError
from projectos.store import OrchestrationJob, utc_now_iso

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

VALID_VERDICTS = frozenset({VERDICT_PASS, VERDICT_FAIL, VERDICT_INCONCLUSIVE})

ASSURANCE_RESULT_MARKER = "PROJECTOS_ASSURANCE_RESULT"


class AssuranceValidationError(OrchestrationError):
    """Raised when an assurance worker result fails server-side validation."""


@dataclass(frozen=True)
class AssuranceFinding:
    finding_id: str
    category: str
    severity: str
    evidence: str
    affected_component: str
    expected_condition: str
    actual_condition: str
    recommended_owner_role: str
    retryable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "category": self.category,
            "severity": self.severity,
            "evidence": self.evidence,
            "affected_component": self.affected_component,
            "expected_condition": self.expected_condition,
            "actual_condition": self.actual_condition,
            "recommended_owner_role": self.recommended_owner_role,
            "retryable": self.retryable,
        }


@dataclass
class AssuranceResult:
    verdict: str
    summary: str
    findings: list[AssuranceFinding] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    candidate_id: str = ""
    candidate_type: str = "git_sha"
    assurance_job_id: str = ""
    assessor_role: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "evidence_refs": self.evidence_refs,
            "candidate_id": self.candidate_id,
            "candidate_type": self.candidate_type,
            "assurance_job_id": self.assurance_job_id,
            "assessor_role": self.assessor_role,
            "completed_at": self.completed_at,
        }


def verdict_to_evidence_result(verdict: str) -> str:
    mapping = {
        VERDICT_PASS: "pass",
        VERDICT_FAIL: "fail",
        VERDICT_INCONCLUSIVE: "inconclusive",
    }
    normalized = str(verdict or "").upper()
    if normalized not in mapping:
        raise AssuranceValidationError(f"Invalid assurance verdict {verdict!r}")
    return mapping[normalized]


def assurance_output_contract(job: OrchestrationJob) -> str:
    candidate = job.source_candidate_sha or job.base_git_sha or ""
    payload = {
        "verdict": "PASS | FAIL | INCONCLUSIVE",
        "summary": "short assessment summary",
        "findings": [
            {
                "finding_id": "FND-EXAMPLE",
                "category": "SOURCE_CODE_DEFECT",
                "severity": "high",
                "evidence": "what was observed",
                "affected_component": "component",
                "expected_condition": "expected",
                "actual_condition": "actual",
                "recommended_owner_role": "SOURCE_CODE_DEFECT",
                "retryable": True,
            }
        ],
        "evidence_refs": [],
        "candidate_id": candidate,
        "candidate_type": "git_sha",
        "assurance_job_id": job.human_id,
        "assessor_role": job.queue,
        "completed_at": "ISO-8601 timestamp",
    }
    return (
        "End your response with a machine-readable assurance verdict block.\n"
        f"Include a line exactly: {ASSURANCE_RESULT_MARKER}\n"
        "Followed by a single JSON object with this schema (no commentary inside JSON):\n"
        f"{json.dumps(payload, indent=2)}\n"
        "Rules:\n"
        "- verdict PASS only when the candidate fully meets acceptance criteria.\n"
        "- verdict FAIL when the candidate has defects; include at least one finding.\n"
        "- verdict INCONCLUSIVE when assessment could not be completed validly.\n"
        "- Worker execution success does not imply PASS.\n"
        f"- candidate_id must be exactly {candidate!r}.\n"
        f"- assurance_job_id must be exactly {job.human_id!r}.\n"
        f"- assessor_role must be exactly {job.queue!r}.\n"
    )


def _parse_findings(raw: Any) -> list[AssuranceFinding]:
    if not isinstance(raw, list):
        return []
    findings: list[AssuranceFinding] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        findings.append(
            AssuranceFinding(
                finding_id=str(item.get("finding_id") or f"FND-{index + 1}"),
                category=str(item.get("category") or "UNKNOWN"),
                severity=str(item.get("severity") or "medium"),
                evidence=str(item.get("evidence") or ""),
                affected_component=str(item.get("affected_component") or ""),
                expected_condition=str(item.get("expected_condition") or ""),
                actual_condition=str(item.get("actual_condition") or ""),
                recommended_owner_role=str(item.get("recommended_owner_role") or item.get("category") or "UNKNOWN"),
                retryable=bool(item.get("retryable", True)),
            )
        )
    return findings


def parse_assurance_result_payload(stdout: str | None) -> dict[str, Any] | None:
    """Extract untrusted assurance JSON from worker stdout."""
    if not stdout or not stdout.strip():
        return None
    marker_index = stdout.find(ASSURANCE_RESULT_MARKER)
    if marker_index >= 0:
        brace = stdout.find("{", marker_index)
        if brace >= 0:
            try:
                payload, _ = json.JSONDecoder().raw_decode(stdout[brace:])
                if isinstance(payload, dict):
                    return payload
            except json.JSONDecodeError:
                pass
    fenced = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        stdout,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        try:
            payload = json.loads(fenced.group(1))
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
    return None


def build_assurance_result(payload: dict[str, Any]) -> AssuranceResult:
    verdict = str(payload.get("verdict") or "").upper()
    refs = payload.get("evidence_refs") or []
    if not isinstance(refs, list):
        refs = []
    return AssuranceResult(
        verdict=verdict,
        summary=str(payload.get("summary") or ""),
        findings=_parse_findings(payload.get("findings")),
        evidence_refs=[str(ref) for ref in refs],
        candidate_id=str(payload.get("candidate_id") or ""),
        candidate_type=str(payload.get("candidate_type") or "git_sha"),
        assurance_job_id=str(payload.get("assurance_job_id") or ""),
        assessor_role=str(payload.get("assessor_role") or ""),
        completed_at=str(payload.get("completed_at") or utc_now_iso()),
    )


def validate_assurance_result(result: AssuranceResult, assurance: OrchestrationJob) -> AssuranceResult:
    """Validate untrusted worker output before recording QA evidence."""
    if result.verdict not in VALID_VERDICTS:
        raise AssuranceValidationError(f"Invalid verdict {result.verdict!r}")

    expected_candidate = assurance.source_candidate_sha or assurance.base_git_sha or ""
    if not expected_candidate:
        raise AssuranceValidationError("Assurance job missing source candidate")
    if result.candidate_id != expected_candidate:
        raise AssuranceValidationError(
            f"Candidate mismatch: result {result.candidate_id!r} != job {expected_candidate!r}"
        )
    if result.assurance_job_id != assurance.human_id:
        raise AssuranceValidationError(
            f"Job mismatch: result {result.assurance_job_id!r} != job {assurance.human_id!r}"
        )
    if result.assessor_role != assurance.queue:
        raise AssuranceValidationError(
            f"Role mismatch: result {result.assessor_role!r} != job {assurance.queue!r}"
        )
    if result.verdict == VERDICT_FAIL and not result.findings and not result.summary:
        raise AssuranceValidationError("FAIL verdict requires findings or summary evidence")
    return result


def parse_and_validate_assurance_result(
    stdout: str | None,
    assurance: OrchestrationJob,
) -> AssuranceResult:
    payload = parse_assurance_result_payload(stdout)
    if payload is None:
        raise AssuranceValidationError("Missing structured assurance verdict")
    result = build_assurance_result(payload)
    return validate_assurance_result(result, assurance)


def format_assurance_stdout(result: AssuranceResult) -> str:
    """Serialize a structured verdict for worker stdout simulation in tests."""
    return f"{ASSURANCE_RESULT_MARKER}\n{json.dumps(result.to_dict())}"


def assurance_result_for_test(
    *,
    verdict: str,
    assurance: OrchestrationJob,
    summary: str = "",
    findings: list[dict[str, Any]] | None = None,
) -> AssuranceResult:
    """Explicit test helper — never used in production runtime paths."""
    candidate = assurance.source_candidate_sha or assurance.base_git_sha or ""
    return validate_assurance_result(
        AssuranceResult(
            verdict=str(verdict).upper(),
            summary=summary or f"test verdict {verdict}",
            findings=_parse_findings(findings or []),
            evidence_refs=[],
            candidate_id=candidate,
            candidate_type="git_sha",
            assurance_job_id=assurance.human_id,
            assessor_role=assurance.queue,
            completed_at=datetime.now(timezone.utc).isoformat(),
        ),
        assurance,
    )
