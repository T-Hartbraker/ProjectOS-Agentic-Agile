"""Resolve authoritative delivery policy for terminal acceptance."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from projectos.delivery.contract import DeliveryContract, load_delivery_contract
from projectos.paths import DEFAULT_REGISTRY_PATH
from projectos.registry import load_registry


def resolve_delivery_policy(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    registry_path: Path | str | None = None,
) -> tuple[DeliveryContract | None, str | None, str | None]:
    """Return (contract, repository_root, error_message).

    For governed RELEASE completion, policy must be resolved deterministically.
    """
    _ = conn
    reg_path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    try:
        registry = load_registry(reg_path)
    except Exception as exc:
        return None, None, f"DELIVERY_POLICY_UNAVAILABLE: {exc}"
    entry = registry.get(project_id)
    if entry is None:
        return None, None, "DELIVERY_POLICY_UNAVAILABLE: project not in registry"
    repo_root = str(entry.repository_root.resolve())
    try:
        contract = load_delivery_contract(entry.repository_root)
    except Exception as exc:
        return None, repo_root, f"DELIVERY_POLICY_UNAVAILABLE: {exc}"
    return contract, repo_root, None


def policy_resolution_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    project_id: str,
    registry_path: Path | str | None = None,
) -> tuple[DeliveryContract | None, str | None, dict[str, Any]]:
    """Resolve policy with evidence payload for terminal evaluation."""
    contract, repo_root, error = resolve_delivery_policy(
        conn, project_id=project_id, registry_path=registry_path
    )
    evidence: dict[str, Any] = {
        "project_id": project_id,
        "run_id": run_id,
        "repository_root": repo_root,
    }
    if error:
        evidence["error"] = error
    return contract, repo_root, evidence
