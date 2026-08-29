"""Effective acceptance contract — Sponsor requirements ∪ delivery policy."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from projectos.delivery.contract import DeliveryContract
from projectos.delivery.gates import GATE_STATUS_PASSED
from projectos.delivery.store import get_delivery_release, list_delivery_artifacts, list_gate_statuses
from projectos.run_evidence import _handoff_desired_outputs


@dataclass
class AcceptanceContract:
    request_type: str
    sponsor_requirements: list[str] = field(default_factory=list)
    policy_requirements: list[str] = field(default_factory=list)
    effective_requirements: list[str] = field(default_factory=list)
    desired_outputs: dict[str, Any] = field(default_factory=dict)
    invalid: bool = False
    invalid_reason: str | None = None


def _policy_requirements(contract: DeliveryContract | None) -> list[str]:
    if contract is None:
        return ["release_record", "verified_artifact", "verify_gate"]
    reqs = ["release_record", "verified_artifact", "verify_gate", "qa_gate"]
    if contract.sbom_policy == "required":
        reqs.append("sbom")
    if contract.checksum_policy == "sha256":
        reqs.append("checksum")
    if contract.code_signing_policy == "required_for_production":
        reqs.append("signature")
    return reqs


def build_acceptance_contract(
    conn: sqlite3.Connection,
    *,
    handoff_id: str | None,
    request_type: str,
    objective: str,
    contract: DeliveryContract | None = None,
) -> AcceptanceContract:
    desired = _handoff_desired_outputs(conn, handoff_id)
    sponsor: list[str] = []
    if desired.get("package") or desired.get("artifact"):
        sponsor.append("package")
    if desired.get("installer"):
        sponsor.append("installer")
    if desired.get("publish") or desired.get("download_link") or desired.get("return_download_link"):
        sponsor.append("publication")
    if desired.get("signed") or desired.get("signature"):
        sponsor.append("signature")
    if desired.get("sbom"):
        sponsor.append("sbom")
    if desired.get("checksum"):
        sponsor.append("checksum")

    if not sponsor and not desired:
        lowered = objective.lower()
        if "publish" in lowered or "download" in lowered or "github" in lowered:
            sponsor.append("publication")
        if "package" in lowered or "artifact" in lowered:
            sponsor.append("package")
        if "installer" in lowered:
            sponsor.append("installer")

    policy = _policy_requirements(contract)
    effective = list(dict.fromkeys(sponsor + policy))

    if str(request_type or "").upper() == "RELEASE":
        baseline = ["release_record", "verified_artifact", "verify_gate"]
        effective = list(dict.fromkeys(baseline + effective))
        if not effective:
            return AcceptanceContract(
                request_type=request_type,
                sponsor_requirements=sponsor,
                policy_requirements=policy,
                effective_requirements=baseline,
                desired_outputs=desired,
                invalid=True,
                invalid_reason="INVALID_OR_INCOMPLETE_ACCEPTANCE_CONTRACT",
            )

    if not effective:
        return AcceptanceContract(
            request_type=request_type,
            sponsor_requirements=sponsor,
            policy_requirements=policy,
            effective_requirements=[],
            desired_outputs=desired,
            invalid=True,
            invalid_reason="INVALID_OR_INCOMPLETE_ACCEPTANCE_CONTRACT",
        )

    return AcceptanceContract(
        request_type=request_type,
        sponsor_requirements=sponsor,
        policy_requirements=policy,
        effective_requirements=effective,
        desired_outputs=desired,
    )


def _artifact_map(conn: sqlite3.Connection, release_record_id: str) -> dict[str, list[dict[str, Any]]]:
    rows = list_delivery_artifacts(conn, release_record_id)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["artifact_type"]), []).append(dict(row))
    return grouped


def evaluate_effective_requirements(
    conn: sqlite3.Connection,
    *,
    contract: AcceptanceContract,
    release_record_id: str | None,
    candidate_git_sha: str | None = None,
    contract_obj: DeliveryContract | None = None,
) -> tuple[bool, list[str], list[str], list[str], dict[str, Any]]:
    required = list(contract.effective_requirements)
    satisfied: list[str] = []
    missing: list[str] = []
    evidence: dict[str, Any] = {
        "sponsor_requirements": contract.sponsor_requirements,
        "policy_requirements": contract.policy_requirements,
        "effective_requirements": contract.effective_requirements,
        "desired_outputs": contract.desired_outputs,
    }

    if contract.invalid:
        missing.append("acceptance_contract")
        evidence["invalid_reason"] = contract.invalid_reason
        return False, required, satisfied, missing, evidence

    record = get_delivery_release(conn, release_record_id=release_record_id) if release_record_id else None
    artifacts: dict[str, list[dict[str, Any]]] = {}
    gates: dict[str, str] = {}
    if record is not None:
        artifacts = _artifact_map(conn, str(record["release_record_id"]))
        gates = list_gate_statuses(conn, str(record["release_record_id"]))
        evidence["release_record_id"] = record["release_record_id"]
        evidence["release_candidate_sha"] = record.get("candidate_git_sha")

    def _req(name: str, ok: bool, ref: Any = None) -> None:
        if name not in required:
            return
        if ok:
            satisfied.append(name)
            if ref is not None:
                evidence[name] = ref
        else:
            missing.append(name)

    _req("release_record", record is not None, record["release_record_id"] if record else None)

    if candidate_git_sha and record is not None:
        _req(
            "candidate_provenance",
            str(record.get("candidate_git_sha") or "") == candidate_git_sha,
            {"expected": candidate_git_sha, "actual": record.get("candidate_git_sha")},
        )

    distributables = (
        artifacts.get("zip")
        or artifacts.get("package")
        or artifacts.get("installer")
        or artifacts.get("installer_placeholder")
        or []
    )
    _req(
        "verified_artifact",
        bool(distributables) and gates.get("VERIFY_GATE") == GATE_STATUS_PASSED,
        {"artifacts": [a.get("artifact_id") for a in distributables], "verify_gate": gates.get("VERIFY_GATE")},
    )
    _req("verify_gate", gates.get("VERIFY_GATE") == GATE_STATUS_PASSED, gates.get("VERIFY_GATE"))
    _req("qa_gate", gates.get("QA_GATE") == GATE_STATUS_PASSED, gates.get("QA_GATE"))

    if "package" in required:
        pkg_ok = bool(distributables)
        _req("package", pkg_ok, [a.get("artifact_id") for a in distributables])

    if "installer" in required:
        installers = artifacts.get("installer") or []
        placeholders = artifacts.get("installer_placeholder") or []
        _req("installer", bool(installers) and not placeholders, installers)

    if "checksum" in required:
        arts = sum(artifacts.values(), [])
        _req(
            "checksum",
            all(a.get("sha256") for a in arts) and gates.get("CHECKSUM_GATE") == GATE_STATUS_PASSED,
            gates.get("CHECKSUM_GATE"),
        )

    if "sbom" in required:
        _req(
            "sbom",
            bool(artifacts.get("sbom")) and gates.get("SBOM_GATE") in {GATE_STATUS_PASSED, "not_required"},
            artifacts.get("sbom"),
        )

    if "signature" in required:
        arts = sum(artifacts.values(), [])
        all_signed = all(str(a.get("signature_status") or "") in {"signed", "not_required"} for a in arts)
        sig_gate = gates.get("SIGNATURE_GATE")
        _req(
            "signature",
            all_signed and sig_gate == GATE_STATUS_PASSED,
            {"gate": sig_gate, "artifacts": [a.get("signature_status") for a in arts]},
        )

    if "publication" in required:
        pub_status = str(record.get("publication_status") or "") if record else ""
        url = str(record.get("github_release_url") or record.get("download_url") or "") if record else ""
        _req(
            "publication",
            pub_status == "published" and bool(url),
            {"publication_status": pub_status, "url": url},
        )
    elif record is not None and "publication" not in required:
        _req(
            "local_release",
            str(record.get("lifecycle_status") or "") in {"packaged", "verified", "local_complete", "released"},
            record.get("lifecycle_status"),
        )

    return not missing, required, satisfied, missing, evidence
