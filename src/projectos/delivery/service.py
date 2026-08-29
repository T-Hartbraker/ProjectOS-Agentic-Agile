"""Universal software delivery orchestration service."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from projectos.build_executor import LocalBuildExecutor, select_build_executor
from projectos.db import connection
from projectos.delivery.contract import DeliveryContract, load_delivery_contract
from projectos.delivery.gates import (
    GATE_STATUS_FAILED,
    GATE_STATUS_NOT_REQUIRED,
    GATE_STATUS_PASSED,
    GATE_STATUS_PENDING,
    GATE_STATUS_SKIPPED,
    all_required_gates_passed,
    blocking_gates,
)
from projectos.delivery.manifest import build_release_manifest, manifest_json, validate_release_manifest
from projectos.delivery.semver import format_tag, parse_semver
from projectos.delivery.events import (
    emit_checksum_created,
    emit_package_completed,
    emit_package_failed,
    emit_package_started,
    emit_publication_started,
    emit_release_blocked,
    emit_release_prepared,
    emit_release_published,
    emit_sbom_created,
    emit_signature_status,
    emit_source_gate_passed,
)
from projectos.candidate_workspace import candidate_workspace
from projectos.delivery.qa_verification import require_qa_gate_passed
from projectos.errors import OrchestrationError
from projectos.delivery.store import (
    append_delivery_audit,
    get_delivery_release,
    insert_delivery_artifact,
    insert_delivery_release,
    list_delivery_artifacts,
    list_delivery_releases,
    list_gate_details,
    list_gate_statuses,
    new_artifact_id,
    new_build_id,
    new_release_record_id,
    update_delivery_artifact,
    update_delivery_release,
    upsert_gate_status,
)
from projectos.github.client import GitHubClient
from projectos.github.tokens import resolve_github_credentials
from projectos.migrate import initialize_database
from projectos.packaging.registry import detect_packaging_adapter, get_adapter
from projectos.paths import STATE_DIR
from projectos.registry import load_registry
from projectos.services.context import ServiceContext

BUILD_ROOT = STATE_DIR / "delivery" / "builds"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _primary_distributable_artifact(artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for artifact_type in ("installer", "installer_placeholder", "zip", "package"):
        match = next((row for row in artifacts if row["artifact_type"] == artifact_type), None)
        if match is not None:
            return match
    return None


def _git_head_sha(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OrchestrationError("Could not resolve git SHA for source gate")
    return completed.stdout.strip()


def _resolve_repo_root(ctx: ServiceContext, project_human_id: str) -> Path:
    registry = load_registry(ctx.registry_path)
    entry = registry.get(project_human_id)
    if entry is None or not entry.enabled:
        raise OrchestrationError(f"Project {project_human_id!r} is not registered or enabled")
    return Path(entry.repository_root).resolve()


class DeliveryService:
    def __init__(
        self,
        ctx: ServiceContext,
        *,
        github_client: GitHubClient | None = None,
        slack_poster: Callable[[str], None] | None = None,
    ) -> None:
        self.ctx = ctx
        self.github = github_client or GitHubClient()
        self.slack_poster = slack_poster

    def show_contract(self, project_human_id: str) -> dict[str, Any]:
        repo_root = _resolve_repo_root(self.ctx, project_human_id)
        contract = load_delivery_contract(repo_root)
        adapter_id = detect_packaging_adapter(repo_root, contract)
        return {
            "project_human_id": project_human_id,
            "contract": self._contract_dict(contract),
            "detected_adapter": adapter_id,
        }

    def validate_contract(self, project_human_id: str) -> dict[str, Any]:
        repo_root = _resolve_repo_root(self.ctx, project_human_id)
        contract = load_delivery_contract(repo_root)
        adapter_id = detect_packaging_adapter(repo_root, contract)
        adapter = get_adapter(adapter_id)
        adapter.validate_environment(repo_root, contract)
        if contract.github_release_enabled:
            creds = resolve_github_credentials(refresh=True)
            if not creds["configured"]:
                raise OrchestrationError("GitHub release is enabled but GitHub is not configured")
            self.github.validate_repository(contract.repository_owner, contract.repository_name)
        return {"ok": True, "adapter": adapter_id, "repository": contract.repository_slug}

    def prepare_release(
        self,
        project_human_id: str,
        *,
        release_human_id: str,
        version: str,
        candidate_git_sha: str | None = None,
        run_id: str | None = None,
        proposal_id: str | None = None,
        sponsor_user_id: str | None = None,
        event_context: EventContext | None = None,
        require_qa_evidence: bool = True,
    ) -> dict[str, Any]:
        parse_semver(version)
        repo_root = _resolve_repo_root(self.ctx, project_human_id)
        contract = load_delivery_contract(repo_root)
        self.validate_contract(project_human_id)
        if require_qa_evidence and not candidate_git_sha:
            raise OrchestrationError(
                "Release preparation requires explicit QA-approved candidate_git_sha"
            )
        git_sha = candidate_git_sha or _git_head_sha(repo_root)
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            existing = get_delivery_release(
                conn,
                project_human_id=project_human_id,
                release_human_id=release_human_id,
            )
            if existing:
                if str(existing.get("candidate_git_sha") or "") != git_sha:
                    raise OrchestrationError(
                        "Existing release record is for a different candidate SHA"
                    )
                if str(existing.get("version") or "") != version:
                    raise OrchestrationError(
                        "Existing release record is for a different version"
                    )
                return self._release_view(conn, existing)
            if require_qa_evidence:
                qa_facts = require_qa_gate_passed(
                    conn,
                    project_id=project_human_id,
                    candidate_git_sha=git_sha,
                    run_id=run_id or (event_context.run_id if event_context else None),
                )
            else:
                qa_facts = {}
            release_record_id = new_release_record_id()
            record = insert_delivery_release(
                conn,
                release_record_id=release_record_id,
                project_human_id=project_human_id,
                release_human_id=release_human_id,
                version=version,
                candidate_git_sha=git_sha,
                lifecycle_status="qa_passed",
                proposal_id=proposal_id,
                sponsor_user_id=sponsor_user_id,
                signing_required=contract.code_signing_policy == "required_for_production",
                sbom_required=contract.sbom_policy == "required",
                github_enabled=contract.github_release_enabled,
            )
            upsert_gate_status(
                conn,
                release_record_id=release_record_id,
                gate_name="QA_GATE",
                status=GATE_STATUS_PASSED,
                detail="QA gate verified from authoritative qa_evidence",
                evidence=qa_facts,
            )
            upsert_gate_status(
                conn,
                release_record_id=release_record_id,
                gate_name="SOURCE_GATE",
                status=GATE_STATUS_PASSED,
                detail="Candidate SHA recorded",
                evidence={"git_sha": git_sha},
            )
            append_delivery_audit(
                conn,
                project_human_id=project_human_id,
                release_record_id=release_record_id,
                event_type="release_prepared",
                actor=sponsor_user_id,
                proposal_id=proposal_id,
                detail=f"Prepared {release_human_id} v{version}",
                evidence={"git_sha": git_sha},
            )
            emit_source_gate_passed(
                conn,
                project_id=project_human_id,
                release_human_id=release_human_id,
                release_record_id=release_record_id,
                git_sha=git_sha,
                event_context=event_context,
            )
            emit_release_prepared(
                conn,
                project_id=project_human_id,
                release_human_id=release_human_id,
                release_record_id=release_record_id,
                git_sha=git_sha,
                event_context=event_context,
            )
            return self._release_view(conn, record)

    def package_release(
        self,
        release_record_id: str,
        *,
        executor: str = "LOCAL",
        event_context: EventContext | None = None,
    ) -> dict[str, Any]:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            record = get_delivery_release(conn, release_record_id=release_record_id)
            if record is None:
                raise OrchestrationError(f"Release record {release_record_id!r} not found")
            project_human_id = str(record["project_human_id"])
            repo_root = _resolve_repo_root(self.ctx, project_human_id)
            contract = load_delivery_contract(repo_root)
            adapter_id = detect_packaging_adapter(repo_root, contract)
            adapter = get_adapter(adapter_id)
            emit_package_started(
                conn,
                project_id=project_human_id,
                release_human_id=str(record["release_human_id"]),
                release_record_id=release_record_id,
                adapter_id=adapter_id,
                event_context=event_context,
            )
            build_id = new_build_id()
            build_dir = BUILD_ROOT / release_record_id / "work"
            output_dir = BUILD_ROOT / release_record_id / "out"
            if build_dir.exists():
                shutil.rmtree(build_dir, ignore_errors=True)
            if output_dir.exists():
                shutil.rmtree(output_dir, ignore_errors=True)
            candidate_sha = str(record["candidate_git_sha"])
            try:
                with candidate_workspace(
                    repo_root, candidate_sha, parent_dir=BUILD_ROOT / release_record_id
                ) as workspace:
                    executor_impl = select_build_executor(
                        prefer_ci=executor.upper() == "CI",
                        ci_available=False,
                    )
                    build_result = executor_impl.run_release_build(
                        repository=contract.repository_slug,
                        git_sha=candidate_sha,
                        version=str(record["version"]),
                        workflow_inputs={"build_id": build_id, "release_record_id": release_record_id},
                    )
                    adapter.validate_environment(workspace, contract)
                    adapter.build(workspace, contract, git_sha=candidate_sha, build_dir=build_dir)
                    upsert_gate_status(
                        conn,
                        release_record_id=release_record_id,
                        gate_name="BUILD_GATE",
                        status=GATE_STATUS_PASSED,
                        detail=build_result.detail,
                        evidence={**(build_result.evidence or {}), "workspace_sha": candidate_sha},
                    )
                    result = adapter.package(
                        workspace,
                        contract,
                        version=str(record["version"]),
                        git_sha=candidate_sha,
                        build_dir=build_dir,
                        output_dir=output_dir,
                    )
                    adapter.verify(result, contract)
                upsert_gate_status(
                    conn,
                    release_record_id=release_record_id,
                    gate_name="PACKAGE_GATE",
                    status=GATE_STATUS_PASSED,
                    detail=result.detail,
                )
                artifacts = []
                for item in result.artifacts:
                    row = insert_delivery_artifact(
                        conn,
                        artifact_id=new_artifact_id(),
                        release_record_id=release_record_id,
                        project_human_id=project_human_id,
                        artifact_name=item.artifact_name,
                        artifact_type=item.artifact_type,
                        platform=item.platform,
                        architecture=item.architecture,
                        version=str(record["version"]),
                        source_git_sha=str(record["candidate_git_sha"]),
                        build_id=build_id,
                        build_timestamp=_now(),
                        local_build_path=str(item.local_path),
                        sha256=item.sha256,
                        size_bytes=item.size_bytes,
                        signature_status=item.signature_status,
                        signature_identity=item.signature_identity,
                    )
                    artifacts.append(row)
                upsert_gate_status(
                    conn,
                    release_record_id=release_record_id,
                    gate_name="CHECKSUM_GATE",
                    status=GATE_STATUS_PASSED,
                    detail="SHA-256 recorded for distributable artifacts",
                    evidence={"artifacts": [row["artifact_id"] for row in artifacts]},
                )
                emit_checksum_created(
                    conn,
                    project_id=project_human_id,
                    release_human_id=str(record["release_human_id"]),
                    artifact_ids=[row["artifact_id"] for row in artifacts],
                    event_context=event_context,
                )
                if contract.sbom_policy == "required" and result.sbom_path and result.sbom_path.is_file():
                    sbom_sha = _sha256_file(result.sbom_path)
                    insert_delivery_artifact(
                        conn,
                        artifact_id=new_artifact_id(),
                        release_record_id=release_record_id,
                        project_human_id=project_human_id,
                        artifact_name=result.sbom_path.name,
                        artifact_type="sbom",
                        platform=contract.target_platforms[0],
                        architecture="",
                        version=str(record["version"]),
                        source_git_sha=str(record["candidate_git_sha"]),
                        build_id=build_id,
                        build_timestamp=_now(),
                        local_build_path=str(result.sbom_path),
                        sha256=sbom_sha,
                        size_bytes=result.sbom_path.stat().st_size,
                        provenance_status="generated",
                    )
                    upsert_gate_status(
                        conn,
                        release_record_id=release_record_id,
                        gate_name="SBOM_GATE",
                        status=GATE_STATUS_PASSED,
                        detail="SBOM generated",
                        evidence={"sha256": sbom_sha},
                    )
                    emit_sbom_created(
                        conn,
                        project_id=project_human_id,
                        release_human_id=str(record["release_human_id"]),
                        sha256=sbom_sha,
                        event_context=event_context,
                    )
                elif contract.sbom_policy != "required":
                    upsert_gate_status(
                        conn,
                        release_record_id=release_record_id,
                        gate_name="SBOM_GATE",
                        status=GATE_STATUS_NOT_REQUIRED,
                    )
                signing_status = (
                    GATE_STATUS_PASSED
                    if contract.code_signing_policy == "not_required"
                    else GATE_STATUS_PENDING
                )
                if contract.code_signing_policy == "required_for_production":
                    unsigned = [
                        row
                        for row in artifacts
                        if str(row["signature_status"]) in {"unsigned", "not_configured"}
                    ]
                    if unsigned:
                        signing_status = GATE_STATUS_PENDING
                    else:
                        signing_status = GATE_STATUS_PASSED
                elif contract.code_signing_policy == "optional":
                    signing_status = GATE_STATUS_PASSED
                upsert_gate_status(
                    conn,
                    release_record_id=release_record_id,
                    gate_name="SIGNATURE_GATE",
                    status=signing_status,
                    detail="Signing policy evaluated",
                )
                emit_signature_status(
                    conn,
                    project_id=project_human_id,
                    release_human_id=str(record["release_human_id"]),
                    status=str(signing_status),
                    detail="Signing policy evaluated",
                    event_context=event_context,
                )
                update_delivery_release(
                    conn,
                    release_record_id,
                    build_executor=executor.upper(),
                    build_id=build_id,
                )
                append_delivery_audit(
                    conn,
                    project_human_id=project_human_id,
                    release_record_id=release_record_id,
                    event_type="release_packaged",
                    detail=f"Packaged with adapter {adapter_id}",
                    evidence={"build_id": build_id},
                )
                emit_package_completed(
                    conn,
                    project_id=project_human_id,
                    release_human_id=str(record["release_human_id"]),
                    release_record_id=release_record_id,
                    adapter_id=adapter_id,
                    artifacts=artifacts,
                    event_context=event_context,
                )
                return self._release_view(conn, get_delivery_release(conn, release_record_id=release_record_id) or record)
            except OrchestrationError as exc:
                update_delivery_release(conn, release_record_id, last_error=str(exc))
                upsert_gate_status(
                    conn,
                    release_record_id=release_record_id,
                    gate_name="BUILD_GATE",
                    status=GATE_STATUS_FAILED,
                    detail=str(exc),
                )
                append_delivery_audit(
                    conn,
                    project_human_id=project_human_id,
                    release_record_id=release_record_id,
                    event_type="package_failed",
                    detail=str(exc),
                )
                emit_package_failed(
                    conn,
                    project_id=project_human_id,
                    release_record_id=release_record_id,
                    error=str(exc),
                    event_context=event_context,
                )
                raise

    def verify_release(self, release_record_id: str) -> dict[str, Any]:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            record = get_delivery_release(conn, release_record_id=release_record_id)
            if record is None:
                raise OrchestrationError(f"Release record {release_record_id!r} not found")
            project_human_id = str(record["project_human_id"])
            repo_root = _resolve_repo_root(self.ctx, project_human_id)
            contract = load_delivery_contract(repo_root)
            artifacts = list_delivery_artifacts(conn, release_record_id)
            installer = _primary_distributable_artifact(artifacts)
            if installer is None:
                raise OrchestrationError("No distributable artifact found for verification")
            path = Path(str(installer["local_build_path"]))
            if not path.is_file():
                raise OrchestrationError("Installer artifact file is missing on disk")
            actual = _sha256_file(path)
            if actual != installer["sha256"]:
                upsert_gate_status(
                    conn,
                    release_record_id=release_record_id,
                    gate_name="CHECKSUM_GATE",
                    status=GATE_STATUS_FAILED,
                    detail="Checksum mismatch during verification",
                )
                raise OrchestrationError("Artifact checksum mismatch")
            manifest = build_release_manifest(
                project_id=project_human_id,
                release_id=str(record["release_human_id"]),
                product_name=contract.product_name or project_human_id,
                version=str(record["version"]),
                git_sha=str(record["candidate_git_sha"]),
                build_id=str(record.get("build_id") or ""),
                target_platform=installer["platform"],
                artifact_filename=installer["artifact_name"],
                artifact_sha256=installer["sha256"],
                artifact_size=int(installer["size_bytes"]),
                signature_status=str(installer["signature_status"]),
                sbom_ref=next((row["artifact_name"] for row in artifacts if row["artifact_type"] == "sbom"), None),
                release_status=str(record["lifecycle_status"]),
                build_executor=str(record.get("build_executor") or "LOCAL"),
                repository=contract.repository_slug,
                release_url=record.get("github_release_url"),
            )
            validate_release_manifest(
                manifest,
                expected_project_id=project_human_id,
                expected_release_id=str(record["release_human_id"]),
                expected_version=str(record["version"]),
                expected_git_sha=str(record["candidate_git_sha"]),
                expected_artifact_sha256=installer["sha256"],
            )
            manifest_path = path.parent / "release-manifest.json"
            manifest_bytes = manifest_json(manifest).encode("utf-8")
            manifest_path.write_bytes(manifest_bytes)
            manifest_sha = _sha256_bytes(manifest_bytes)
            update_delivery_release(conn, release_record_id, manifest_sha256=manifest_sha)
            append_delivery_audit(
                conn,
                project_human_id=project_human_id,
                release_record_id=release_record_id,
                event_type="release_verified",
                detail="Manifest validated",
                evidence={"manifest_sha256": manifest_sha},
            )
            upsert_gate_status(
                conn,
                release_record_id=release_record_id,
                gate_name="VERIFY_GATE",
                status=GATE_STATUS_PASSED,
                detail="Release verification completed",
            )
            update_delivery_release(conn, release_record_id, lifecycle_status="verified")
            return self._release_view(conn, get_delivery_release(conn, release_record_id=release_record_id) or record)

    def publish_release(
        self,
        release_record_id: str,
        *,
        proposal_id: str | None = None,
        approval_message_ts: str | None = None,
        event_context: EventContext | None = None,
    ) -> dict[str, Any]:
        initialize_database(self.ctx.db_path)
        announce_view: dict[str, Any] | None = None
        announce_enabled = False
        with connection(self.ctx.db_path) as conn:
            record = get_delivery_release(conn, release_record_id=release_record_id)
            if record is None:
                raise OrchestrationError(f"Release record {release_record_id!r} not found")
            if str(record.get("publication_status") or "") == "published":
                return self._release_view(conn, record)
            project_human_id = str(record["project_human_id"])
            emit_publication_started(
                conn,
                project_id=project_human_id,
                release_human_id=str(record["release_human_id"]),
                release_record_id=release_record_id,
                event_context=event_context,
            )
            repo_root = _resolve_repo_root(self.ctx, project_human_id)
            contract = load_delivery_contract(repo_root)
            announce_enabled = contract.slack_release_announcement_enabled
            gates = list_gate_statuses(conn, release_record_id)
            if gates.get("VERIFY_GATE") not in {GATE_STATUS_PASSED, GATE_STATUS_NOT_REQUIRED, None}:
                if gates.get("VERIFY_GATE") != GATE_STATUS_PASSED:
                    raise OrchestrationError("Release verification required before publication")
            if gates.get("SIGNATURE_GATE") == GATE_STATUS_FAILED:
                raise OrchestrationError("Signature gate failed; publication blocked")
            if gates.get("SIGNATURE_GATE") == GATE_STATUS_PENDING and contract.code_signing_policy == "required_for_production":
                raise OrchestrationError("Signature gate is pending; production publication blocked")
            blocked = [gate for gate in blocking_gates(gates) if gate not in {"PUBLICATION_GATE", "DELIVERY_GATE"}]
            if blocked:
                raise OrchestrationError(f"Release gates blocking publication: {', '.join(blocked)}")
            if not contract.github_release_enabled:
                upsert_gate_status(
                    conn,
                    release_record_id=release_record_id,
                    gate_name="PUBLICATION_GATE",
                    status=GATE_STATUS_NOT_REQUIRED,
                )
                update_delivery_release(
                    conn,
                    release_record_id,
                    publication_status="local_complete",
                    lifecycle_status="local_complete",
                )
                view = self._release_view(conn, get_delivery_release(conn, release_record_id=release_record_id) or record)
                announce_view = view
            else:
                artifacts = list_delivery_artifacts(conn, release_record_id)
                assets: dict[str, tuple[bytes, str]] = {}
                for row in artifacts:
                    path = Path(str(row["local_build_path"]))
                    if not path.is_file():
                        raise OrchestrationError(f"Missing artifact file: {row['artifact_name']}")
                    content_type = "application/octet-stream"
                    if row["artifact_type"] == "sbom":
                        content_type = "application/json"
                    assets[row["artifact_name"]] = (path.read_bytes(), content_type)
                manifest_path = next(
                    (Path(str(row["local_build_path"])).parent / "release-manifest.json" for row in artifacts if row["artifact_type"] == "installer"),
                    None,
                )
                if manifest_path and manifest_path.is_file():
                    assets["release-manifest.json"] = (manifest_path.read_bytes(), "application/json")
                tag = format_tag(str(record["version"]))
                try:
                    update_delivery_release(conn, release_record_id, publication_status="in_progress")
                    publication = self.github.publish_release_assets(
                        contract.repository_owner,
                        contract.repository_name,
                        tag=tag,
                        title=f"{contract.product_name or project_human_id} {record['version']}",
                        body=f"ProjectOS release {record['release_human_id']}",
                        target_commitish=str(record["candidate_git_sha"]),
                        assets=assets,
                    )
                    for row in artifacts:
                        url = publication.asset_urls.get(row["artifact_name"])
                        if url:
                            update_delivery_artifact(
                                conn,
                                str(row["artifact_id"]),
                                published_url=url,
                                publication_status="published",
                            )
                    upsert_gate_status(
                        conn,
                        release_record_id=release_record_id,
                        gate_name="PUBLICATION_GATE",
                        status=GATE_STATUS_PASSED,
                        detail=publication.detail,
                        evidence={"release_url": publication.release_url},
                    )
                    update_delivery_release(
                        conn,
                        release_record_id,
                        publication_status="published",
                        lifecycle_status="released",
                        github_release_url=publication.release_url,
                        github_tag=tag,
                        proposal_id=proposal_id or record.get("proposal_id"),
                        approval_message_ts=approval_message_ts,
                    )
                    append_delivery_audit(
                        conn,
                        project_human_id=project_human_id,
                        release_record_id=release_record_id,
                        event_type="release_published",
                        proposal_id=proposal_id,
                        detail=publication.detail,
                        evidence={"release_url": publication.release_url, "tag": tag},
                    )
                    view = self._release_view(conn, get_delivery_release(conn, release_record_id=release_record_id) or record)
                    announce_view = view
                    emit_release_published(
                        conn,
                        project_id=project_human_id,
                        release_human_id=str(record["release_human_id"]),
                        release_record_id=release_record_id,
                        url=publication.release_url,
                        event_context=event_context,
                    )
                except OrchestrationError as exc:
                    update_delivery_release(conn, release_record_id, publication_status="failed", last_error=str(exc))
                    emit_release_blocked(
                        conn,
                        project_id=project_human_id,
                        release_human_id=str(record["release_human_id"]),
                        reason=str(exc),
                        event_context=event_context,
                    )
                    upsert_gate_status(
                        conn,
                        release_record_id=release_record_id,
                        gate_name="PUBLICATION_GATE",
                        status=GATE_STATUS_FAILED,
                        detail=str(exc),
                    )
                    raise
        if announce_enabled and announce_view is not None:
            self._announce_slack(announce_view)
            with connection(self.ctx.db_path) as conn:
                update_delivery_release(conn, release_record_id, slack_announced=1)
                upsert_gate_status(
                    conn,
                    release_record_id=release_record_id,
                    gate_name="DELIVERY_GATE",
                    status=GATE_STATUS_PASSED if self.slack_poster else GATE_STATUS_PENDING,
                    detail="Slack release card posted" if self.slack_poster else "Slack poster not configured",
                )
        return announce_view or self.get_release(release_record_id)

    def list_releases(self, project_human_id: str) -> dict[str, Any]:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            rows = list_delivery_releases(conn, project_human_id)
            return {
                "project_human_id": project_human_id,
                "releases": [self._release_view(conn, row) for row in rows],
            }

    def get_release(self, release_record_id: str) -> dict[str, Any]:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            record = get_delivery_release(conn, release_record_id=release_record_id)
            if record is None:
                raise OrchestrationError(f"Release record {release_record_id!r} not found")
            return self._release_view(conn, record)

    def release_blockers(self, project_human_id: str, *, release_record_id: str | None = None) -> dict[str, Any]:
        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            if release_record_id:
                record = get_delivery_release(conn, release_record_id=release_record_id)
                if record is None:
                    raise OrchestrationError(f"Release record {release_record_id!r} not found")
                gates = list_gate_statuses(conn, release_record_id)
                return {
                    "project_human_id": project_human_id,
                    "release_record_id": release_record_id,
                    "blocking_gates": blocking_gates(gates),
                    "ready": all_required_gates_passed(gates),
                }
            rows = list_delivery_releases(conn, project_human_id)
            if not rows:
                return {"project_human_id": project_human_id, "blocking_gates": ["NO_RELEASE"], "ready": False}
            latest = rows[0]
            gates = list_gate_statuses(conn, str(latest["release_record_id"]))
            return {
                "project_human_id": project_human_id,
                "release_record_id": latest["release_record_id"],
                "blocking_gates": blocking_gates(gates),
                "ready": all_required_gates_passed(gates),
            }

    def _announce_slack(self, view: dict[str, Any]) -> None:
        installer = _primary_distributable_artifact(view.get("artifacts") or [])
        if installer is None:
            return
        lines = [
            "*ProjectOS — RELEASED*",
            "",
            f"*Project:*\n{view['project_human_id']}",
            "",
            f"*Version:*\n{view['version']}",
            "",
            f"*Platform:*\n{installer.get('platform')}",
            "",
            f"*Installer:*\n<{installer.get('published_url') or installer.get('local_build_path')}|{installer.get('artifact_name')}>",
            "",
            f"*Integrity:*\nSHA-256: `{installer.get('sha256')}`",
            "",
            f"*Signature:*\n{installer.get('signature_status')}",
            "",
            f"*Source:*\n`{view.get('candidate_git_sha')}`",
        ]
        if view.get("github_release_url"):
            lines.extend(["", f"*Release:*\n<{view['github_release_url']}|GitHub Release {view.get('github_tag')}>"])
        sbom = next((item for item in view.get("artifacts") or [] if item.get("artifact_type") == "sbom"), None)
        if sbom and sbom.get("published_url"):
            lines.extend(["", f"*SBOM:*\n<{sbom['published_url']}|Download SBOM>"])
        message = "\n".join(lines)
        if self.slack_poster is not None:
            self.slack_poster(message)

    def _release_view(self, conn, record: dict[str, Any]) -> dict[str, Any]:
        release_record_id = str(record["release_record_id"])
        return {
            **record,
            "gates": list_gate_details(conn, release_record_id),
            "gate_summary": list_gate_statuses(conn, release_record_id),
            "artifacts": list_delivery_artifacts(conn, release_record_id),
            "ready_to_publish": all_required_gates_passed(
                {k: v for k, v in list_gate_statuses(conn, release_record_id).items() if k not in {"PUBLICATION_GATE", "DELIVERY_GATE"}}
            ),
        }

    def _contract_dict(self, contract: DeliveryContract) -> dict[str, Any]:
        return {
            "delivery_type": contract.delivery_type,
            "target_platforms": list(contract.target_platforms),
            "packaging_adapter": contract.packaging_adapter,
            "repository_provider": contract.repository_provider,
            "repository_owner": contract.repository_owner,
            "repository_name": contract.repository_name,
            "default_branch": contract.default_branch,
            "release_strategy": contract.release_strategy,
            "installer_format": contract.installer_format,
            "installer_name_template": contract.installer_name_template,
            "artifact_retention": contract.artifact_retention,
            "code_signing_policy": contract.code_signing_policy,
            "sbom_policy": contract.sbom_policy,
            "checksum_policy": contract.checksum_policy,
            "github_release_enabled": contract.github_release_enabled,
            "slack_release_announcement_enabled": contract.slack_release_announcement_enabled,
            "product_name": contract.product_name,
            "entry_point": contract.entry_point,
        }
