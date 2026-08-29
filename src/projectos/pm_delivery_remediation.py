"""PM-owned recoverable delivery remediation."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from projectos.delivery.contract import (
    delivery_json_path,
    infer_delivery_contract,
    load_delivery_contract,
)
from projectos.domain_events import ACTOR_ARCHITECTURE, ACTOR_DELIVERY, ACTOR_PM, EventContext, emit_projectos_event
from projectos.errors import OrchestrationError
from projectos.run_evidence import pause_run_for_sponsor_decision

RECOVERABLE_CONFIGURATION = "RECOVERABLE_CONFIGURATION"
RECOVERABLE_IMPLEMENTATION = "RECOVERABLE_IMPLEMENTATION"
SPONSOR_DECISION_REQUIRED = "SPONSOR_DECISION_REQUIRED"
UNRECOVERABLE_EXTERNAL = "UNRECOVERABLE_EXTERNAL"

_GITHUB_REMOTE_RE = re.compile(
    r"(?:github\.com[:/]|git@github\.com:)(?P<owner>[^/]+)/(?P<name>[^/.]+)"
)


@dataclass(frozen=True)
class DeliveryRemediationResult:
    recovered: bool
    recoverability: str
    message: str
    sponsor_pause: bool = False
    contract_path: str | None = None


def classify_failure_recoverability(failure: dict[str, Any]) -> str:
    blocker = str(failure.get("blocker_type") or "").upper()
    if blocker == "DELIVERY_CONTRACT_MISSING":
        return RECOVERABLE_CONFIGURATION
    if blocker in {"CAPABILITY_GAP", "INSTALLER_BACKEND_MISSING"}:
        return RECOVERABLE_IMPLEMENTATION
    if failure.get("sponsor_decision_required"):
        return SPONSOR_DECISION_REQUIRED
    if failure.get("retryable") is False:
        return UNRECOVERABLE_EXTERNAL
    return RECOVERABLE_CONFIGURATION if failure.get("retryable") else UNRECOVERABLE_EXTERNAL


def _infer_repo_metadata(repo_root: Path) -> dict[str, str]:
    product_name = "Project"
    owner = ""
    name = ""
    identity_path = repo_root / "project" / "repository.json"
    if identity_path.is_file():
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            product_name = str(identity.get("project_name") or product_name)
        except json.JSONDecodeError:
            pass
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            match = _GITHUB_REMOTE_RE.search(result.stdout.strip())
            if match:
                owner = match.group("owner")
                name = match.group("name")
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "product_name": product_name,
        "repository_owner": owner,
        "repository_name": name,
    }


def _sponsor_decisions_required(metadata: dict[str, str], draft: dict[str, Any]) -> list[str]:
    decisions: list[str] = []
    if not metadata.get("repository_owner") or not metadata.get("repository_name"):
        decisions.extend(["repository_owner", "repository_name"])
    if draft.get("code_signing_policy") == "required_for_production":
        decisions.append("code_signing_policy")
    if not draft.get("target_platforms"):
        decisions.append("target_platforms")
    return decisions


def apply_governed_delivery_contract(repo_root: Path, contract: dict[str, Any]) -> Path:
    path = delivery_json_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    load_delivery_contract(repo_root)
    return path


def attempt_delivery_contract_remediation(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    repo_root: Path,
    failure: dict[str, Any],
) -> DeliveryRemediationResult:
    """PM evaluates missing delivery contract and attempts governed auto-remediation."""
    recoverability = classify_failure_recoverability(failure)
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="DELIVERY_CONTRACT_MISSING",
        summary="Delivery contract missing; PM evaluating remediation.",
        actor_id=ACTOR_PM,
        phase="RELEASE_PREPARATION",
        detail_level="milestone",
        evidence={**failure, "recoverability": recoverability},
    )

    if recoverability == UNRECOVERABLE_EXTERNAL:
        return DeliveryRemediationResult(
            recovered=False,
            recoverability=recoverability,
            message="Delivery failure is not recoverable without external authority.",
        )

    metadata = _infer_repo_metadata(repo_root)
    draft = infer_delivery_contract(
        product_name=metadata["product_name"],
        repository_owner=metadata["repository_owner"] or "REPLACE_ME",
        repository_name=metadata["repository_name"] or repo_root.name,
        target_platforms=["windows-x64"],
        external_distribution=False,
    )
    sponsor_decisions = _sponsor_decisions_required(metadata, draft)
    remediation_evidence = {
        "recoverability": recoverability,
        "draft_contract": draft,
        "sponsor_decisions_required": sponsor_decisions,
        "inferred_metadata": metadata,
    }

    if sponsor_decisions:
        pause_run_for_sponsor_decision(
            conn,
            event_ctx=event_ctx,
            summary="Sponsor decision required before delivery contract can be applied.",
            detail="ProjectOS generated a governed delivery contract draft but needs Sponsor input.",
            evidence=remediation_evidence,
        )
        return DeliveryRemediationResult(
            recovered=False,
            recoverability=SPONSOR_DECISION_REQUIRED,
            message="Waiting for Sponsor decisions before applying delivery contract.",
            sponsor_pause=True,
        )

    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="PM_REPLAN",
        summary="PM replanned release preparation with inferred delivery contract.",
        actor_id=ACTOR_PM,
        phase="RELEASE_PREPARATION",
        evidence=remediation_evidence,
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="AGENT_ASSIGNED",
        summary="Assigned: delivery-agent to apply governed delivery contract.",
        actor_id=ACTOR_PM,
        phase="RELEASE_PREPARATION",
        metadata={"agent_id": ACTOR_DELIVERY},
    )
    try:
        contract_path = apply_governed_delivery_contract(repo_root, draft)
    except OrchestrationError as exc:
        return DeliveryRemediationResult(
            recovered=False,
            recoverability=recoverability,
            message=str(exc),
        )

    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="WORK_COMPLETED",
        summary="Governed delivery contract applied.",
        actor_id=ACTOR_DELIVERY,
        phase="RELEASE_PREPARATION",
        evidence={"contract_path": str(contract_path)},
    )
    return DeliveryRemediationResult(
        recovered=True,
        recoverability=recoverability,
        message="Governed delivery contract applied.",
        contract_path=str(contract_path),
    )


def handle_capability_gap(
    conn: sqlite3.Connection,
    *,
    event_ctx: EventContext,
    gap: dict[str, Any],
    project_id: str,
    repository_root: str,
    service_ctx=None,
    worker=None,
) -> DeliveryRemediationResult:
    """PM routes capability gaps into executable work or authority escalation."""
    recoverability = classify_failure_recoverability(gap)
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="CAPABILITY_GAP_DETECTED",
        summary=str(gap.get("reason") or "Capability gap detected."),
        actor_id=ACTOR_PM,
        phase="CAPABILITY",
        detail_level="milestone",
        evidence={**gap, "recoverability": recoverability},
    )
    if recoverability == UNRECOVERABLE_EXTERNAL:
        return DeliveryRemediationResult(
            recovered=False,
            recoverability=recoverability,
            message="Capability gap is outside authorized remediation scope.",
        )

    blocker = str(gap.get("blocker_type") or "").upper()
    if blocker in {"INSTALLER_BACKEND_MISSING", "CAPABILITY_GAP"} and "installer" in str(gap.get("reason", "")).lower():
        from projectos.run_evidence import pause_run_for_sponsor_decision

        emit_projectos_event(
            conn,
            ctx=event_ctx,
            event_type="CROSS_PROJECT_REMEDIATION_REQUIRED",
            summary="Installer backend requires ProjectOS maintenance authority.",
            actor_id=ACTOR_PM,
            phase="CAPABILITY",
            evidence=gap,
        )
        pause_run_for_sponsor_decision(
            conn,
            event_ctx=event_ctx,
            summary="Capability gap requires cross-project remediation authorization.",
            evidence=gap,
        )
        return DeliveryRemediationResult(
            recovered=False,
            recoverability=SPONSOR_DECISION_REQUIRED,
            message="Waiting for Sponsor authorization for cross-project capability work.",
            sponsor_pause=True,
        )

    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="PM_REPLAN",
        summary="PM replanned to address capability gap.",
        actor_id=ACTOR_PM,
        phase="CAPABILITY",
        evidence=gap,
    )
    from projectos.remediation_store import create_remediation_work
    from projectos.remediation_executor import execute_remediation_work

    work = create_remediation_work(
        conn,
        run_id=event_ctx.run_id or project_id,
        project_id=project_id,
        remediation_cycle=1,
        finding_ids=[str(gap.get("blocker_type") or "CAPABILITY_GAP")],
        assigned_agent=ACTOR_ARCHITECTURE,
        objective=str(gap.get("reason") or "Address capability gap"),
        acceptance_criteria="Capability implemented and validated within project scope.",
        source_candidate_id=None,
        repository_root=repository_root,
        assignment_reason="Capability gap remediation within project scope",
        findings=[gap],
    )
    emit_projectos_event(
        conn,
        ctx=event_ctx,
        event_type="AGENT_ASSIGNED",
        summary="Assigned: architecture-agent to implement missing capability.",
        actor_id=ACTOR_PM,
        phase="CAPABILITY",
        metadata={"agent_id": ACTOR_ARCHITECTURE, "work_item_id": work.work_item_id},
        evidence={"work_item_id": work.work_item_id, "orchestration_job_id": work.orchestration_job_id},
    )
    execute_remediation_work(
        conn,
        work=work,
        event_ctx=event_ctx,
        project_id=project_id,
        repository_root=repository_root,
        service_ctx=service_ctx,
        worker=worker,
    )
    return DeliveryRemediationResult(
        recovered=False,
        recoverability=RECOVERABLE_IMPLEMENTATION,
        message="PM assigned and executed capability remediation work; run remains active.",
    )
