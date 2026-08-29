"""Release manifest generation and validation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from projectos.errors import OrchestrationError


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_release_manifest(
    *,
    project_id: str,
    release_id: str,
    product_name: str,
    version: str,
    git_sha: str,
    build_id: str,
    target_platform: str,
    artifact_filename: str,
    artifact_sha256: str,
    artifact_size: int,
    signature_status: str,
    sbom_ref: str | None,
    release_status: str,
    build_executor: str,
    repository: str,
    release_url: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "release_id": release_id,
        "product_name": product_name,
        "version": version,
        "git_sha": git_sha,
        "build_id": build_id,
        "build_timestamp": _now(),
        "target_platform": target_platform,
        "artifact": {
            "filename": artifact_filename,
            "sha256": artifact_sha256,
            "size_bytes": artifact_size,
        },
        "signature_status": signature_status,
        "sbom_ref": sbom_ref,
        "projectos_release_status": release_status,
        "build_executor": build_executor,
        "repository": repository,
        "release_url": release_url,
    }


def validate_release_manifest(
    manifest: dict[str, Any],
    *,
    expected_project_id: str,
    expected_release_id: str,
    expected_version: str,
    expected_git_sha: str,
    expected_artifact_sha256: str,
) -> None:
    if not isinstance(manifest, dict):
        raise OrchestrationError("Release manifest must be a JSON object")
    if str(manifest.get("project_id") or "") != expected_project_id:
        raise OrchestrationError("Release manifest project_id mismatch")
    if str(manifest.get("release_id") or "") != expected_release_id:
        raise OrchestrationError("Release manifest release_id mismatch")
    if str(manifest.get("version") or "") != expected_version:
        raise OrchestrationError("Release manifest version mismatch")
    if str(manifest.get("git_sha") or "") != expected_git_sha:
        raise OrchestrationError("Release manifest git_sha mismatch")
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict):
        raise OrchestrationError("Release manifest missing artifact block")
    if str(artifact.get("sha256") or "") != expected_artifact_sha256:
        raise OrchestrationError("Release manifest artifact SHA-256 mismatch")


def manifest_json(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"
