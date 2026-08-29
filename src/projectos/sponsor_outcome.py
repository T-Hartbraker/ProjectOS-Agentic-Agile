"""Sponsor outcome verification before RUN_COMPLETED."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from projectos.delivery.gates import GATE_STATUS_PASSED
from projectos.delivery.store import get_delivery_release, list_delivery_artifacts, list_gate_statuses
from projectos.run_evidence import _handoff_desired_outputs


@dataclass
class SponsorOutcomeEvaluation:
    satisfied: bool
    required_outputs: list[str] = field(default_factory=list)
    satisfied_outputs: list[str] = field(default_factory=list)
    missing_outputs: list[str] = field(default_factory=list)
    evidence_refs: dict[str, Any] = field(default_factory=dict)


def _artifact_map(conn: sqlite3.Connection, release_record_id: str) -> dict[str, list[dict[str, Any]]]:
    rows = list_delivery_artifacts(conn, release_record_id)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["artifact_type"]), []).append(dict(row))
    return grouped


def evaluate_sponsor_outcome(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    handoff_id: str | None,
    objective: str,
    release_record_id: str | None = None,
    candidate_git_sha: str | None = None,
) -> SponsorOutcomeEvaluation:
    desired = _handoff_desired_outputs(conn, handoff_id)
    required: list[str] = []
    satisfied: list[str] = []
    missing: list[str] = []
    evidence: dict[str, Any] = {"desired_outputs": desired}

    def _require(name: str, ok: bool, ref: Any = None) -> None:
        required.append(name)
        if ok:
            satisfied.append(name)
            if ref is not None:
                evidence[name] = ref
        else:
            missing.append(name)

    record = None
    if release_record_id:
        record = get_delivery_release(conn, release_record_id=release_record_id)
    artifacts: dict[str, list[dict[str, Any]]] = {}
    gates: dict[str, str] = {}
    if record is not None:
        artifacts = _artifact_map(conn, str(record["release_record_id"]))
        gates = list_gate_statuses(conn, str(record["release_record_id"]))
        evidence["release_record_id"] = record["release_record_id"]
        evidence["release_candidate_sha"] = record.get("candidate_git_sha")

    wants_package = bool(desired.get("package") or desired.get("artifact"))
    wants_installer = bool(desired.get("installer"))
    wants_publish = bool(desired.get("publish") or desired.get("download_link") or desired.get("return_download_link"))
    wants_signed = bool(desired.get("signed") or desired.get("signature"))
    wants_sbom = bool(desired.get("sbom"))
    wants_checksum = bool(desired.get("checksum"))

    if not desired:
        wants_package = "release" in objective.lower() or "package" in objective.lower()
        wants_publish = any(w in objective.lower() for w in ("publish", "download", "github"))

    if candidate_git_sha and record is not None:
        _require(
            "candidate_provenance",
            str(record.get("candidate_git_sha") or "") == candidate_git_sha,
            {"expected": candidate_git_sha, "actual": record.get("candidate_git_sha")},
        )

    if wants_package:
        pkg_ok = bool(artifacts.get("zip") or artifacts.get("package") or artifacts.get("installer") or artifacts.get("installer_placeholder"))
        _require("package", pkg_ok, [a.get("artifact_id") for a in sum(artifacts.values(), [])])

    if wants_installer:
        installers = artifacts.get("installer") or []
        placeholders = artifacts.get("installer_placeholder") or []
        _require("installer", bool(installers) and not placeholders, installers)

    if wants_checksum:
        arts = sum(artifacts.values(), [])
        _require(
            "checksum",
            all(a.get("sha256") for a in arts) and gates.get("CHECKSUM_GATE") == GATE_STATUS_PASSED,
            gates.get("CHECKSUM_GATE"),
        )

    if wants_sbom:
        _require(
            "sbom",
            bool(artifacts.get("sbom")) and gates.get("SBOM_GATE") in {GATE_STATUS_PASSED, "not_required"},
            artifacts.get("sbom"),
        )

    if wants_signed:
        arts = sum(artifacts.values(), [])
        all_signed = all(str(a.get("signature_status") or "") in {"signed", "not_required"} for a in arts)
        sig_gate = gates.get("SIGNATURE_GATE")
        _require(
            "signature",
            all_signed and sig_gate == GATE_STATUS_PASSED,
            {"gate": sig_gate, "artifacts": [a.get("signature_status") for a in arts]},
        )

    if wants_publish:
        pub_status = str(record.get("publication_status") or "") if record else ""
        url = str(record.get("github_release_url") or record.get("download_url") or "") if record else ""
        _require(
            "publication",
            pub_status == "published" and bool(url),
            {"publication_status": pub_status, "url": url},
        )
    elif record is not None and not wants_publish:
        _require(
            "local_release",
            str(record.get("lifecycle_status") or "") in {"packaged", "verified", "local_complete", "released"},
            record.get("lifecycle_status"),
        )

    if gates:
        _require(
            "qa_gate",
            gates.get("QA_GATE") == GATE_STATUS_PASSED,
            gates.get("QA_GATE"),
        )

    return SponsorOutcomeEvaluation(
        satisfied=not missing,
        required_outputs=required,
        satisfied_outputs=satisfied,
        missing_outputs=missing,
        evidence_refs=evidence,
    )
