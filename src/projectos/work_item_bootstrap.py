"""Authoritative projectctl work-item creation from PM plan output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from projectos.errors import OrchestrationError
from projectos.projectctl_bridge import (
    create_projectctl_entity,
    ensure_iteration,
    read_work_item_ids,
    show_work_item,
)

_CREATABLE_TYPES = frozenset({"story", "requirement", "defect"})


@dataclass
class WorkItemBootstrapResult:
    plan: dict[str, Any]
    id_map: dict[str, str] = field(default_factory=dict)
    known_work_items: dict[str, set[str]] = field(default_factory=dict)
    created: list[tuple[str, str]] = field(default_factory=list)


def _map_key(work_item_type: str, provisional_id: str) -> str:
    return f"{work_item_type}:{provisional_id}"


def _job_title(job: dict[str, Any]) -> str:
    for key in ("title", "scope_summary", "requirement_ref"):
        value = job.get(key)
        if value and str(value).strip():
            return str(value).strip()[:200]
    wi_type = str(job.get("work_item_type") or "story")
    provisional = str(job.get("provisional_work_item_ref") or job.get("work_item_human_id") or "")
    return f"Governed {wi_type} {provisional}".strip()[:200]


def _job_description(job: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("scope_summary", "requirement_ref"):
        value = job.get(key)
        if value and str(value).strip():
            parts.append(str(value).strip())
    acceptance = job.get("acceptance_criteria")
    if isinstance(acceptance, list):
        for item in acceptance:
            text = str(item).strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()[:4000]


def _provisional_id(job: dict[str, Any]) -> str | None:
    explicit = str(job.get("provisional_work_item_ref") or "").strip()
    if explicit:
        return explicit
    value = str(job.get("work_item_human_id") or "").strip()
    return value or None


def _job_needs_bootstrap(
    job: dict[str, Any],
    *,
    known: dict[str, set[str]],
    repository_root,
    python_executable,
) -> bool:
    wi_type = str(job.get("work_item_type") or "").strip().lower()
    provisional = _provisional_id(job)
    if not wi_type or not provisional or wi_type not in _CREATABLE_TYPES:
        return False
    if provisional in known.get(wi_type, set()):
        return False
    title = _job_title(job)
    if _resolve_existing_by_title(
        repository_root=repository_root,
        work_item_type=wi_type,
        title=title,
        known=known,
        python_executable=python_executable,
    ):
        return False
    return True


def _resolve_existing_by_title(
    *,
    repository_root,
    work_item_type: str,
    title: str,
    known: dict[str, set[str]],
    python_executable,
) -> str | None:
    for hid in sorted(known.get(work_item_type, set())):
        shown = show_work_item(
            repository_root,
            work_item_type,
            hid,
            python_executable=python_executable,
        )
        if shown and str(shown.get("title") or "").strip() == title:
            return hid
    return None


def _create_work_item(
    *,
    repository_root,
    work_item_type: str,
    title: str,
    description: str,
    known: dict[str, set[str]],
    python_executable,
    projectctl_runner=None,
) -> str:
    existing = _resolve_existing_by_title(
        repository_root=repository_root,
        work_item_type=work_item_type,
        title=title,
        known=known,
        python_executable=python_executable,
    )
    if existing:
        return existing

    if work_item_type == "defect":
        from projectos.projectctl_bridge import create_defect

        result = create_defect(
            repository_root,
            title=title,
            description=description or None,
            python_executable=python_executable,
        )
    else:
        result = create_projectctl_entity(
            repository_root,
            work_item_type,
            title=title,
            description=description or None,
            python_executable=python_executable,
        )
    _ = projectctl_runner  # reserved for test injection parity

    for line in (result.stdout or "").splitlines():
        if line.startswith("Created "):
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]

    refreshed = read_work_item_ids(
        repository_root, python_executable=python_executable
    )
    created = refreshed.get(work_item_type, set()) - known.get(work_item_type, set())
    if len(created) == 1:
        return next(iter(created))
    raise OrchestrationError(
        f"Could not determine created {work_item_type} id for {title!r}: {result.stdout}"
    )


def bootstrap_plan_work_items(
    plan: dict[str, Any],
    *,
    repository_root,
    python_executable,
    known_work_items: dict[str, set[str]] | None = None,
    projectctl_runner=None,
    create_fn: Callable[..., str] | None = None,
) -> WorkItemBootstrapResult:
    """Persist authoritative work items for PM plan references before job creation."""
    known = {
        key: set(values)
        for key, values in (known_work_items or {}).items()
    }
    id_map: dict[str, str] = {}
    created: list[tuple[str, str]] = []
    creator = create_fn or _create_work_item

    jobs = plan.get("jobs")
    if not isinstance(jobs, list):
        return WorkItemBootstrapResult(plan=plan, id_map=id_map, known_work_items=known)

    pending_jobs = [
        job
        for job in jobs
        if isinstance(job, dict)
        and _job_needs_bootstrap(
            job,
            known=known,
            repository_root=repository_root,
            python_executable=python_executable,
        )
    ]
    if not pending_jobs:
        return WorkItemBootstrapResult(plan=plan, id_map=id_map, known_work_items=known)

    iteration_id = str(plan.get("iteration_human_id") or "").strip()
    if iteration_id and iteration_id not in known.get("iteration", set()):
        ensure_iteration(
            repository_root,
            iteration_id,
            name=f"Iteration {iteration_id}",
            python_executable=python_executable,
        )
        known = read_work_item_ids(
            repository_root, python_executable=python_executable
        )

    for job in pending_jobs:
        wi_type = str(job.get("work_item_type") or "").strip().lower()
        provisional = _provisional_id(job)
        if not wi_type or not provisional:
            continue

        map_key = _map_key(wi_type, provisional)
        if map_key in id_map:
            job["work_item_human_id"] = id_map[map_key]
            continue

        bucket = known.get(wi_type, set())
        if provisional in bucket:
            id_map[map_key] = provisional
            job["work_item_human_id"] = provisional
            continue

        title = _job_title(job)
        existing = _resolve_existing_by_title(
            repository_root=repository_root,
            work_item_type=wi_type,
            title=title,
            known=known,
            python_executable=python_executable,
        )
        if existing:
            id_map[map_key] = existing
            job["work_item_human_id"] = existing
            bucket.add(existing)
            known[wi_type] = bucket
            continue

        authoritative_id = creator(
            repository_root=repository_root,
            work_item_type=wi_type,
            title=title,
            description=_job_description(job),
            known=known,
            python_executable=python_executable,
            projectctl_runner=projectctl_runner,
        )
        id_map[map_key] = authoritative_id
        job["work_item_human_id"] = authoritative_id
        created.append((wi_type, authoritative_id))
        bucket.add(authoritative_id)
        known[wi_type] = bucket

    if id_map:
        plan = dict(plan)
        plan["work_item_id_map"] = dict(id_map)
    return WorkItemBootstrapResult(
        plan=plan,
        id_map=id_map,
        known_work_items=known,
        created=created,
    )
