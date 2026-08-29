"""Read-only release center. Downloads are allowlisted by release/artifact ID."""

from __future__ import annotations

from typing import Any

from projectos.errors import OrchestrationError
from projectos.store import (
    OrchestrationJob,
    get_release_artifact,
    list_release_artifacts,
    require_safe_id,
)

_READY_OUTCOMES = frozenset({"GATE_READY", "READY"})
_REJECTED_OUTCOMES = frozenset({"GATE_REJECTED", "REJECTED"})
_TEXT_KINDS = frozenset({"notes", "rollback", "qa_package", "readiness"})


def _safe_or_missing(value: str, *, label: str) -> str:
    try:
        return require_safe_id(value, label=label)
    except OrchestrationError:
        raise OrchestrationError(f"{label} {value!r} not found") from None


def _gate(job: OrchestrationJob) -> str:
    outcome = job.outcome or ""
    if outcome in _READY_OUTCOMES:
        return "ready"
    if outcome in _REJECTED_OUTCOMES or job.status in {"FAILED", "BLOCKED"}:
        return "rejected"
    return "pending"


def _decode_text(content: bytes) -> str:
    return content.decode("utf-8", errors="replace")


def _artifact_public(row: dict[str, Any], project_human_id: str) -> dict[str, Any]:
    release_id = row["release_human_id"]
    artifact_id = row["artifact_human_id"]
    return {
        "artifact_human_id": artifact_id,
        "filename": row["filename"],
        "sha256": row["sha256"],
        "byte_size": row["byte_size"],
        "media_type": row["media_type"],
        "kind": row["kind"],
        "download_ref": (
            f"/v1/projects/{project_human_id}/releases/{release_id}/artifacts/{artifact_id}"
        ),
    }


def _qa_recommendation(quality: dict[str, Any], gate: str) -> dict[str, Any]:
    reasons = list(quality.get("release_blocking_reasons") or [])
    if gate == "rejected":
        reasons.append("release gate is rejected")
    if reasons:
        return {"status": "do_not_release", "reasons": reasons}
    if gate == "ready":
        return {"status": "recommend_release", "reasons": []}
    return {"status": "pending", "reasons": ["release gate has not finished"]}


def _known_findings(quality: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    findings = quality.get("findings") or {}
    for key in ("security", "quality"):
        finding = findings.get(key)
        if finding:
            items.append(
                {
                    "kind": key,
                    "result": finding.get("result"),
                    "role": finding.get("role"),
                    "candidate_git_sha": finding.get("candidate_git_sha"),
                    "evidence_ref": finding.get("evidence_ref"),
                }
            )
    for defect in quality.get("defects") or []:
        items.append(
            {
                "kind": "defect",
                "result": defect.get("result"),
                "role": defect.get("assurance_role"),
                "candidate_git_sha": defect.get("candidate_git_sha"),
                "evidence_ref": defect.get("defect_human_id"),
            }
        )
    return items


def _notes_from_artifacts(
    conn, project_human_id: str, release_human_id: str
) -> tuple[str | None, str | None]:
    notes = None
    rollback = None
    for row in list_release_artifacts(conn, project_human_id, release_human_id):
        if row["kind"] not in _TEXT_KINDS:
            continue
        blob = get_release_artifact(
            conn,
            project_human_id=project_human_id,
            release_human_id=release_human_id,
            artifact_human_id=row["artifact_human_id"],
        )
        if blob is None:
            continue
        text = _decode_text(blob["content"])
        if row["kind"] == "notes" and notes is None:
            notes = text
        elif row["kind"] == "rollback" and rollback is None:
            rollback = text
    return notes, rollback


def _release_summary(
    job: OrchestrationJob,
    *,
    integrated_sha: str | None,
    quality: dict[str, Any],
    artifact_count: int,
) -> dict[str, Any]:
    gate = _gate(job)
    rec = _qa_recommendation(quality, gate)
    released = job.candidate_git_sha if gate == "ready" and job.status == "SUCCEEDED" else None
    return {
        "release_human_id": job.human_id,
        "job_human_id": job.human_id,
        "iteration_human_id": job.iteration_human_id,
        "status": job.status,
        "outcome": job.outcome,
        "gate": gate,
        "integrated_sha": integrated_sha or job.source_candidate_sha,
        "released_sha": released,
        "qa_recommendation": rec["status"],
        "artifact_count": artifact_count,
        "updated_at": job.updated_at,
    }


def build_release_list(
    *,
    project_human_id: str,
    jobs: list[OrchestrationJob],
    integrations: list[dict[str, Any]],
    quality: dict[str, Any],
    artifacts_by_release: dict[str, int],
) -> dict[str, Any]:
    latest_integration = integrations[0] if integrations else None
    integrated = (latest_integration or {}).get("integrated_sha")
    releases = [
        _release_summary(
            job,
            integrated_sha=integrated,
            quality=quality,
            artifact_count=artifacts_by_release.get(job.human_id, 0),
        )
        for job in jobs
        if job.queue == "RELEASE"
    ]
    return {"project_human_id": project_human_id, "releases": releases}


def build_release_detail(
    conn,
    *,
    project_human_id: str,
    release_human_id: str,
    jobs: list[OrchestrationJob],
    integrations: list[dict[str, Any]],
    quality: dict[str, Any],
) -> dict[str, Any]:
    release_id = _safe_or_missing(release_human_id, label="release")
    job = next(
        (item for item in jobs if item.human_id == release_id and item.queue == "RELEASE"),
        None,
    )
    if job is None:
        raise OrchestrationError(f"release {release_id!r} not found")
    latest_integration = integrations[0] if integrations else None
    integrated = (latest_integration or {}).get("integrated_sha") or job.source_candidate_sha
    catalog = list_release_artifacts(conn, project_human_id, release_id)
    public_artifacts = [_artifact_public(row, project_human_id) for row in catalog]
    notes, rollback = _notes_from_artifacts(conn, project_human_id, release_id)
    rec = _qa_recommendation(quality, _gate(job))
    summary = _release_summary(
        job,
        integrated_sha=integrated,
        quality=quality,
        artifact_count=len(public_artifacts),
    )
    return {
        **summary,
        "project_human_id": project_human_id,
        "qa_recommendation_detail": rec,
        "known_findings": _known_findings(quality),
        "release_notes": notes,
        "migration_notes": None,
        "rollback_notes": rollback,
        "manifest": {
            "release_human_id": release_id,
            "integrated_sha": summary["integrated_sha"],
            "released_sha": summary["released_sha"],
            "files": [
                {
                    "artifact_human_id": item["artifact_human_id"],
                    "filename": item["filename"],
                    "sha256": item["sha256"],
                    "byte_size": item["byte_size"],
                }
                for item in public_artifacts
            ],
        },
        "checksums": [
            {"filename": item["filename"], "sha256": item["sha256"]}
            for item in public_artifacts
        ],
        "artifacts": public_artifacts,
        "last_error": job.last_error,
    }


def load_release_artifact_bytes(
    conn,
    *,
    project_human_id: str,
    release_human_id: str,
    artifact_human_id: str,
    jobs: list[OrchestrationJob],
) -> dict[str, Any]:
    release_id = _safe_or_missing(release_human_id, label="release")
    artifact_id = _safe_or_missing(artifact_human_id, label="artifact")
    job = next(
        (item for item in jobs if item.human_id == release_id and item.queue == "RELEASE"),
        None,
    )
    if job is None:
        raise OrchestrationError(f"release {release_id!r} not found")
    row = get_release_artifact(
        conn,
        project_human_id=project_human_id,
        release_human_id=release_id,
        artifact_human_id=artifact_id,
    )
    if row is None:
        raise OrchestrationError(f"artifact {artifact_id!r} not found")
    return row
