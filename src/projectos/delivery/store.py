"""Delivery pipeline persistence."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from projectos.delivery.gates import RELEASE_GATES, initial_gate_statuses

RELEASE_RECORD_PREFIX = "DLV-"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_release_record_id() -> str:
    return f"{RELEASE_RECORD_PREFIX}{uuid.uuid4().hex[:12].upper()}"


def new_artifact_id() -> str:
    return f"ART-{uuid.uuid4().hex[:12].upper()}"


def new_build_id() -> str:
    return f"BLD-{uuid.uuid4().hex[:12].upper()}"


def _row_to_release(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def insert_delivery_release(
    conn: sqlite3.Connection,
    *,
    release_record_id: str,
    project_human_id: str,
    release_human_id: str,
    version: str,
    candidate_git_sha: str,
    lifecycle_status: str = "qa_passed",
    build_executor: str | None = None,
    proposal_id: str | None = None,
    sponsor_user_id: str | None = None,
    signing_required: bool = False,
    sbom_required: bool = True,
    github_enabled: bool = True,
) -> dict[str, Any]:
    conn.execute(
        """
        INSERT INTO delivery_releases (
            release_record_id, project_human_id, release_human_id, version,
            candidate_git_sha, lifecycle_status, build_executor, proposal_id,
            sponsor_user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            release_record_id,
            project_human_id,
            release_human_id,
            version,
            candidate_git_sha,
            lifecycle_status,
            build_executor,
            proposal_id,
            sponsor_user_id,
        ),
    )
    for gate, status in initial_gate_statuses(
        signing_required=signing_required,
        sbom_required=sbom_required,
        github_enabled=github_enabled,
    ).items():
        upsert_gate_status(conn, release_record_id=release_record_id, gate_name=gate, status=status)
    return get_delivery_release(conn, release_record_id=release_record_id) or {}


def get_delivery_release(
    conn: sqlite3.Connection,
    *,
    release_record_id: str | None = None,
    project_human_id: str | None = None,
    release_human_id: str | None = None,
) -> dict[str, Any] | None:
    if release_record_id:
        row = conn.execute(
            "SELECT * FROM delivery_releases WHERE release_record_id = ?",
            (release_record_id,),
        ).fetchone()
        return _row_to_release(row) if row else None
    if project_human_id and release_human_id:
        row = conn.execute(
            """
            SELECT * FROM delivery_releases
            WHERE project_human_id = ? AND release_human_id = ?
            """,
            (project_human_id, release_human_id),
        ).fetchone()
        return _row_to_release(row) if row else None
    return None


def list_delivery_releases(conn: sqlite3.Connection, project_human_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM delivery_releases
        WHERE project_human_id = ?
        ORDER BY created_at DESC
        """,
        (project_human_id,),
    ).fetchall()
    return [_row_to_release(row) for row in rows]


def update_delivery_release(conn: sqlite3.Connection, release_record_id: str, **fields: Any) -> None:
    allowed = {
        "version",
        "candidate_git_sha",
        "lifecycle_status",
        "build_executor",
        "build_id",
        "publication_status",
        "github_release_url",
        "github_tag",
        "manifest_sha256",
        "slack_announced",
        "last_error",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    updates["updated_at"] = _now()
    columns = ", ".join(f"{key} = ?" for key in updates)
    conn.execute(
        f"UPDATE delivery_releases SET {columns} WHERE release_record_id = ?",
        (*updates.values(), release_record_id),
    )


def upsert_gate_status(
    conn: sqlite3.Connection,
    *,
    release_record_id: str,
    gate_name: str,
    status: str,
    detail: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO delivery_gate_status (release_record_id, gate_name, status, detail, evidence_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(release_record_id, gate_name) DO UPDATE SET
            status = excluded.status,
            detail = excluded.detail,
            evidence_json = excluded.evidence_json,
            updated_at = excluded.updated_at
        """,
        (
            release_record_id,
            gate_name,
            status,
            detail,
            json.dumps(evidence) if evidence else None,
            _now(),
        ),
    )


def list_gate_statuses(conn: sqlite3.Connection, release_record_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT gate_name, status FROM delivery_gate_status WHERE release_record_id = ?",
        (release_record_id,),
    ).fetchall()
    out = {gate: "pending" for gate in RELEASE_GATES}
    for row in rows:
        out[str(row["gate_name"])] = str(row["status"])
    return out


def list_gate_details(conn: sqlite3.Connection, release_record_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT gate_name, status, detail, evidence_json, updated_at
        FROM delivery_gate_status WHERE release_record_id = ?
        ORDER BY gate_name
        """,
        (release_record_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        evidence = None
        if row["evidence_json"]:
            try:
                evidence = json.loads(row["evidence_json"])
            except json.JSONDecodeError:
                evidence = None
        out.append(
            {
                "gate_name": row["gate_name"],
                "status": row["status"],
                "detail": row["detail"],
                "evidence": evidence,
                "updated_at": row["updated_at"],
            }
        )
    return out


def insert_delivery_artifact(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    release_record_id: str,
    project_human_id: str,
    artifact_name: str,
    artifact_type: str,
    platform: str,
    architecture: str,
    version: str,
    source_git_sha: str,
    build_id: str,
    build_timestamp: str,
    local_build_path: str,
    sha256: str,
    size_bytes: int,
    signature_status: str = "not_configured",
    signature_identity: str | None = None,
    sbom_url: str | None = None,
    provenance_status: str = "recorded",
    publication_status: str = "pending",
    published_url: str | None = None,
) -> dict[str, Any]:
    conn.execute(
        """
        INSERT INTO delivery_artifacts (
            artifact_id, release_record_id, project_human_id, artifact_name, artifact_type,
            platform, architecture, version, source_git_sha, build_id, build_timestamp,
            local_build_path, published_url, sha256, size_bytes, signature_status,
            signature_identity, sbom_url, provenance_status, publication_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            release_record_id,
            project_human_id,
            artifact_name,
            artifact_type,
            platform,
            architecture,
            version,
            source_git_sha,
            build_id,
            build_timestamp,
            local_build_path,
            published_url,
            sha256,
            size_bytes,
            signature_status,
            signature_identity,
            sbom_url,
            provenance_status,
            publication_status,
        ),
    )
    row = conn.execute(
        "SELECT * FROM delivery_artifacts WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()
    return dict(row)


def get_delivery_artifact(conn: sqlite3.Connection, artifact_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM delivery_artifacts WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()
    return dict(row) if row else None


def list_delivery_artifacts(conn: sqlite3.Connection, release_record_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM delivery_artifacts
        WHERE release_record_id = ?
        ORDER BY created_at ASC
        """,
        (release_record_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def update_delivery_artifact(conn: sqlite3.Connection, artifact_id: str, **fields: Any) -> None:
    allowed = {
        "published_url",
        "publication_status",
        "signature_status",
        "signature_identity",
        "sbom_url",
        "provenance_status",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    updates["updated_at"] = _now()
    columns = ", ".join(f"{key} = ?" for key in updates)
    conn.execute(
        f"UPDATE delivery_artifacts SET {columns} WHERE artifact_id = ?",
        (*updates.values(), artifact_id),
    )


def append_delivery_audit(
    conn: sqlite3.Connection,
    *,
    project_human_id: str,
    event_type: str,
    release_record_id: str | None = None,
    actor: str | None = None,
    proposal_id: str | None = None,
    detail: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO delivery_audit_log (
            release_record_id, project_human_id, event_type, actor, proposal_id, detail, evidence_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            release_record_id,
            project_human_id,
            event_type,
            actor,
            proposal_id,
            detail,
            json.dumps(evidence) if evidence else None,
        ),
    )
