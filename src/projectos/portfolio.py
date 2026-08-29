"""Cross-project portfolio summary. Does not merge project stores."""

from __future__ import annotations

from typing import Any

from projectos.errors import OrchestrationError
from projectos.services.context import ServiceContext
from projectos.services.facades import ProjectQueryService, RegistryService


def _health(summary, jobs, quality: dict[str, Any]) -> str:
    if not summary.enabled:
        return "disabled"
    blockers = quality.get("release_blocking_reasons") or []
    failed = sum(1 for job in jobs if job.status == "FAILED")
    blocked = sum(1 for job in jobs if job.status == "BLOCKED")
    if blockers or blocked:
        return "blocked"
    if failed:
        return "degraded"
    return "healthy"


def build_portfolio(ctx: ServiceContext) -> dict[str, Any]:
    registry = RegistryService(ctx)
    queries = ProjectQueryService(ctx)
    cards: list[dict[str, Any]] = []
    for entry in registry.list_projects():
        project = entry.project_human_id
        try:
            summary = queries.summary(project)
            jobs = queries.jobs(project)
            quality = queries.quality(project)
            learning = queries.learning(project)
        except OrchestrationError:
            cards.append(
                {
                    "project_human_id": project,
                    "enabled": bool(entry.enabled),
                    "health": "unknown",
                    "current_iteration_human_id": None,
                    "blocker_count": 0,
                    "release_human_id": None,
                    "release_status": None,
                    "active_job_count": 0,
                    "open_defect_count": 0,
                    "active_memory_count": 0,
                }
            )
            continue
        active = sum(
            1 for job in jobs if job.status in {"READY", "RUNNING", "QUEUED", "LEASED"}
        )
        open_defects = [
            item for item in (quality.get("defects") or []) if item.get("status") == "open"
        ]
        cards.append(
            {
                "project_human_id": project,
                "enabled": bool(summary.enabled),
                "health": _health(summary, jobs, quality),
                "current_iteration_human_id": summary.current_iteration_human_id,
                "blocker_count": len(quality.get("release_blocking_reasons") or [])
                + sum(1 for job in jobs if job.status == "BLOCKED"),
                "release_human_id": summary.current_release_job_human_id,
                "release_status": summary.current_release_status,
                "active_job_count": active,
                "open_defect_count": len(open_defects),
                "active_memory_count": len(learning.get("active_memories") or []),
            }
        )
    return {
        "notice": (
            "Portfolio is the only normal cross-project view. "
            "Each card is loaded from that project's own records."
        ),
        "projects": cards,
    }
