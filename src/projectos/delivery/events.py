"""Delivery pipeline domain event emission at mutation boundary."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

from projectos.domain_events import (
    ACTOR_DELIVERY,
    ACTOR_PM,
    ACTOR_RELEASE,
    EventContext,
    emit_projectos_event,
    lookup_event_context_for_project,
)


def _ctx_or_lookup(
    conn: sqlite3.Connection,
    project_id: str,
    event_context: EventContext | None,
) -> EventContext | None:
    if event_context is not None:
        return event_context
    return lookup_event_context_for_project(conn, project_id)


def emit_source_gate_passed(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    release_human_id: str,
    release_record_id: str,
    git_sha: str,
    event_context: EventContext | None = None,
) -> None:
    ctx = _ctx_or_lookup(conn, project_id, event_context)
    if ctx is None:
        return
    emit_projectos_event(
        conn,
        ctx=replace(ctx, release_id=release_human_id, release_record_id=release_record_id),
        event_type="SOURCE_GATE_PASSED",
        summary="Source gate passed.",
        actor_id=ACTOR_DELIVERY,
        phase="SOURCE_GATE",
        status="PASSED",
        detail_level="normal",
        evidence={"git_sha": git_sha, "release_record_id": release_record_id},
    )


def emit_release_prepared(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    release_human_id: str,
    release_record_id: str,
    git_sha: str,
    event_context: EventContext | None = None,
) -> None:
    ctx = _ctx_or_lookup(conn, project_id, event_context)
    if ctx is None:
        return
    ctx = replace(ctx, release_id=release_human_id, release_record_id=release_record_id)
    emit_projectos_event(
        conn,
        ctx=ctx,
        event_type="RELEASE_CANDIDATE",
        summary=f"Release candidate prepared: {release_human_id}",
        actor_id=ACTOR_DELIVERY,
        phase="SOURCE_GATE",
        detail_level="normal",
        evidence={"release_record_id": release_record_id, "git_sha": git_sha},
    )


def emit_package_started(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    release_human_id: str,
    release_record_id: str,
    adapter_id: str,
    event_context: EventContext | None = None,
) -> None:
    ctx = _ctx_or_lookup(conn, project_id, event_context)
    if ctx is None:
        return
    emit_projectos_event(
        conn,
        ctx=replace(ctx, release_id=release_human_id),
        event_type="PACKAGE_STARTED",
        summary="Package build started.",
        actor_id=ACTOR_DELIVERY,
        phase="BUILD_GATE",
        detail_level="normal",
        evidence={"adapter": adapter_id, "release_record_id": release_record_id},
    )


def emit_package_completed(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    release_human_id: str,
    release_record_id: str,
    adapter_id: str,
    artifacts: list[dict[str, Any]],
    event_context: EventContext | None = None,
) -> None:
    ctx = _ctx_or_lookup(conn, project_id, event_context)
    if ctx is None:
        return
    installer = next((a for a in artifacts if a.get("artifact_type") == "installer"), None)
    stub = adapter_id == "python_desktop"
    evidence: dict[str, Any] = {
        "release_record_id": release_record_id,
        "adapter": adapter_id,
        "artifact_count": len(artifacts),
    }
    if installer:
        evidence.update(
            {
                "artifact_name": installer.get("artifact_name"),
                "sha256": installer.get("sha256"),
                "size_bytes": installer.get("size_bytes"),
                "signature_status": installer.get("signature_status"),
            }
        )
    emit_projectos_event(
        conn,
        ctx=replace(ctx, release_id=release_human_id),
        event_type="PACKAGE_COMPLETED",
        summary=f"Package gate complete for {release_human_id}.",
        actor_id=ACTOR_DELIVERY,
        phase="PACKAGE_GATE",
        detail_level="milestone",
        evidence=evidence,
    )
    if stub:
        emit_projectos_event(
            conn,
            ctx=replace(ctx, release_id=release_human_id),
            event_type="CAPABILITY_GAP_DETECTED",
            summary="Installer backend unavailable; placeholder artifact produced.",
            actor_id=ACTOR_PM,
            phase="CAPABILITY",
            detail_level="milestone",
            evidence={
                "stub_installer": True,
                "adapter": adapter_id,
                "blocker_type": "INSTALLER_BACKEND_MISSING",
                "retryable": True,
            },
        )
    elif installer:
        emit_projectos_event(
            conn,
            ctx=replace(ctx, release_id=release_human_id),
            event_type="INSTALLER_BUILT",
            summary=f"Installer built: {installer.get('artifact_name')}",
            actor_id=ACTOR_DELIVERY,
            phase="PACKAGE_GATE",
            detail_level="milestone",
            evidence=evidence,
        )


def emit_checksum_created(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    release_human_id: str,
    artifact_ids: list[str],
    event_context: EventContext | None = None,
) -> None:
    ctx = _ctx_or_lookup(conn, project_id, event_context)
    if ctx is None:
        return
    emit_projectos_event(
        conn,
        ctx=replace(ctx, release_id=release_human_id),
        event_type="CHECKSUM_CREATED",
        summary="Checksums recorded for distributable artifacts.",
        actor_id=ACTOR_DELIVERY,
        phase="CHECKSUM_GATE",
        detail_level="normal",
        evidence={"artifact_ids": artifact_ids},
    )


def emit_sbom_created(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    release_human_id: str,
    sha256: str,
    event_context: EventContext | None = None,
) -> None:
    ctx = _ctx_or_lookup(conn, project_id, event_context)
    if ctx is None:
        return
    emit_projectos_event(
        conn,
        ctx=replace(ctx, release_id=release_human_id),
        event_type="SBOM_CREATED",
        summary="SBOM generated.",
        actor_id=ACTOR_DELIVERY,
        phase="SBOM_GATE",
        detail_level="normal",
        evidence={"sha256": sha256},
    )


def emit_signature_status(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    release_human_id: str,
    status: str,
    detail: str = "",
    event_context: EventContext | None = None,
) -> None:
    ctx = _ctx_or_lookup(conn, project_id, event_context)
    if ctx is None:
        return
    emit_projectos_event(
        conn,
        ctx=replace(ctx, release_id=release_human_id),
        event_type="SIGNATURE_STATUS",
        summary=f"Signature gate: {status}.",
        actor_id=ACTOR_DELIVERY,
        phase="SIGNATURE_GATE",
        status=status,
        detail=detail[:500],
        detail_level="normal",
    )


def emit_publication_started(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    release_human_id: str,
    release_record_id: str,
    event_context: EventContext | None = None,
) -> None:
    ctx = _ctx_or_lookup(conn, project_id, event_context)
    if ctx is None:
        return
    emit_projectos_event(
        conn,
        ctx=replace(ctx, release_id=release_human_id, release_record_id=release_record_id),
        event_type="PUBLICATION_STARTED",
        summary="Publication started.",
        actor_id=ACTOR_RELEASE,
        phase="PUBLICATION_GATE",
        detail_level="normal",
    )


def emit_package_failed(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    release_record_id: str,
    error: str,
    event_context: EventContext | None = None,
) -> None:
    ctx = _ctx_or_lookup(conn, project_id, event_context)
    if ctx is None:
        return
    emit_projectos_event(
        conn,
        ctx=ctx,
        event_type="PACKAGE_FAILED",
        summary="Package pipeline failed.",
        actor_id=ACTOR_DELIVERY,
        phase="BUILD_GATE",
        status="FAILED",
        detail=error[:500],
        detail_level="milestone",
        evidence={"release_record_id": release_record_id},
        visibility="SPONSOR",
    )


def emit_release_published(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    release_human_id: str,
    release_record_id: str,
    url: str | None,
    event_context: EventContext | None = None,
) -> None:
    ctx = _ctx_or_lookup(conn, project_id, event_context)
    if ctx is None:
        return
    emit_projectos_event(
        conn,
        ctx=replace(ctx, release_id=release_human_id, release_record_id=release_record_id),
        event_type="RELEASE_PUBLISHED",
        summary=f"Release published: {release_human_id}",
        actor_id=ACTOR_RELEASE,
        phase="PUBLICATION_GATE",
        status="COMPLETED",
        detail_level="milestone",
        evidence={"url": url, "release_record_id": release_record_id},
    )


def emit_release_blocked(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    release_human_id: str,
    reason: str,
    event_context: EventContext | None = None,
) -> None:
    ctx = _ctx_or_lookup(conn, project_id, event_context)
    if ctx is None:
        return
    emit_projectos_event(
        conn,
        ctx=replace(ctx, release_id=release_human_id),
        event_type="RELEASE_BLOCKED",
        summary="Publication blocked.",
        actor_id=ACTOR_RELEASE,
        phase="PUBLICATION_GATE",
        status="BLOCKED",
        detail=reason[:500],
        detail_level="milestone",
    )
