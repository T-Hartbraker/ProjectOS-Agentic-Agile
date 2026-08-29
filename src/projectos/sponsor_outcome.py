"""Sponsor outcome verification before RUN_COMPLETED."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from projectos.acceptance_contract import (
    AcceptanceContract,
    build_acceptance_contract,
    evaluate_effective_requirements,
)
from projectos.delivery.contract import load_delivery_contract
from projectos.execution_run import get_execution_run


@dataclass
class SponsorOutcomeEvaluation:
    satisfied: bool
    required_outputs: list[str] = field(default_factory=list)
    satisfied_outputs: list[str] = field(default_factory=list)
    missing_outputs: list[str] = field(default_factory=list)
    evidence_refs: dict[str, Any] = field(default_factory=dict)
    acceptance_contract: AcceptanceContract | None = None


def evaluate_sponsor_outcome(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    handoff_id: str | None,
    objective: str,
    request_type: str | None = None,
    release_record_id: str | None = None,
    candidate_git_sha: str | None = None,
    repository_root: str | None = None,
) -> SponsorOutcomeEvaluation:
    run = get_execution_run(conn, run_id)
    resolved_request_type = request_type or (run.request_type if run else "") or ""
    contract_obj = None
    if repository_root:
        try:
            from pathlib import Path

            contract_obj = load_delivery_contract(Path(repository_root))
        except Exception:
            contract_obj = None

    acceptance = build_acceptance_contract(
        conn,
        handoff_id=handoff_id,
        request_type=resolved_request_type,
        objective=objective,
        contract=contract_obj,
    )

    if release_record_id is None:
        if str(resolved_request_type).upper() == "RELEASE":
            return SponsorOutcomeEvaluation(
                satisfied=False,
                required_outputs=acceptance.effective_requirements,
                missing_outputs=acceptance.effective_requirements or ["release_record"],
                evidence_refs={
                    "invalid_reason": acceptance.invalid_reason or "missing_release_record",
                    "acceptance_contract": acceptance.effective_requirements,
                },
                acceptance_contract=acceptance,
            )
        if acceptance.invalid:
            return SponsorOutcomeEvaluation(
                satisfied=False,
                required_outputs=acceptance.effective_requirements,
                missing_outputs=["acceptance_contract"],
                evidence_refs={"invalid_reason": acceptance.invalid_reason},
                acceptance_contract=acceptance,
            )
        return SponsorOutcomeEvaluation(
            satisfied=True,
            required_outputs=[],
            satisfied_outputs=[],
            missing_outputs=[],
            evidence_refs={"note": "non_release_run_without_release_record"},
            acceptance_contract=acceptance,
        )

    satisfied, required, ok, missing, evidence = evaluate_effective_requirements(
        conn,
        contract=acceptance,
        release_record_id=release_record_id,
        candidate_git_sha=candidate_git_sha,
        contract_obj=contract_obj,
    )
    return SponsorOutcomeEvaluation(
        satisfied=satisfied,
        required_outputs=required,
        satisfied_outputs=ok,
        missing_outputs=missing,
        evidence_refs=evidence,
        acceptance_contract=acceptance,
    )
