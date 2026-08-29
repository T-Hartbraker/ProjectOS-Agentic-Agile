"""Governed invalidation / rework for erroneous delivery candidates."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from projectos.db import connection
from projectos.errors import OrchestrationError
from projectos.migrate import initialize_database
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.projectctl_bridge import (
    create_projectctl_entity,
    ensure_iteration,
    read_work_item_ids,
    resolve_validated_repo,
    show_work_item,
)
from projectos.store import (
    OrchestrationJob,
    add_job_dependency,
    append_run_event,
    create_job,
    get_job_by_human_id,
    insert_candidate_invalidation,
    mark_cancelled,
    set_job_outcome,
)


@dataclass
class InvalidateResult:
    delivery_human_id: str
    invalidated: bool
    assurance_cancelled: list[str] = field(default_factory=list)
    rework_human_id: str | None = None
    error: str | None = None


@dataclass
class FatReconcileResult:
    project_human_id: str
    iteration_human_id: str
    work_items: dict[str, str] = field(default_factory=dict)
    invalidations: list[InvalidateResult] = field(default_factory=list)
    integration_rewired: bool = False
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(i.invalidated and not i.error for i in self.invalidations)


_ITER002_STORIES = (
    {
        "key": "due_overdue",
        "title": "Task due date and overdue indication",
        "description": (
            "As a user, I can set a due date on a task and see when it is overdue, "
            "while preserving all REL-001 behavior.\n\n"
            "Acceptance Criteria:\n"
            "- AC-DUE-001: A task may be created or updated with an optional due date.\n"
            "- AC-DUE-002: Tasks past their due date and not complete are indicated as overdue.\n"
            "- AC-DUE-003: Existing REL-001 create/edit/complete/persist/filter/UI behavior remains intact.\n"
        ),
        "old_job": "JOB-P2-DEL-DUE-OVERDUE",
        "rework_job": "JOB-P2-DEL-DUE-OVERDUE__REWORK-1",
    },
    {
        "key": "priority_filter",
        "title": "Task priority and priority filtering",
        "description": (
            "As a user, I can assign priority LOW/MEDIUM/HIGH to a task and filter "
            "the list by priority, while preserving all REL-001 behavior.\n\n"
            "Acceptance Criteria:\n"
            "- AC-PRI-001: A task may be created or updated with priority LOW, MEDIUM, or HIGH.\n"
            "- AC-PRI-002: The task list can be filtered by priority.\n"
            "- AC-PRI-003: Existing REL-001 create/edit/complete/persist/filter/UI behavior remains intact.\n"
        ),
        "old_job": "JOB-P2-DEL-PRIORITY-FILTER",
        "rework_job": "JOB-P2-DEL-PRIORITY-FILTER__REWORK-1",
    },
)


def _extract_acs(description: str) -> list[str]:
    lines: list[str] = []
    for line in (description or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("- AC-") or stripped.startswith("AC-"):
            lines.append(stripped.lstrip("- ").strip())
    return lines


def _cancel_assurance_for_delivery(
    conn, delivery: OrchestrationJob, *, reason: str
) -> list[str]:
    cancelled: list[str] = []
    rows = conn.execute(
        """
        SELECT id, human_id, status FROM orchestration_jobs
        WHERE source_delivery_job_id = ?
           OR human_id LIKE ?
        """,
        (delivery.id, f"{delivery.human_id}__%"),
    ).fetchall()
    for row in rows:
        if row["human_id"] == delivery.human_id:
            continue
        status = str(row["status"])
        if status in {"CANCELLED"}:
            cancelled.append(str(row["human_id"]))
            continue
        if status in {"SUCCEEDED", "FAILED", "BLOCKED"}:
            continue
        mark_cancelled(conn, int(row["id"]), reason=reason)
        cancelled.append(str(row["human_id"]))

    conn.execute(
        """
        UPDATE qa_evidence
        SET result = 'stale_rejected',
            evidence_ref = COALESCE(evidence_ref, 'invalidated_candidate')
        WHERE delivery_job_id = ? AND result = 'pending'
        """,
        (delivery.id,),
    )
    return cancelled


def invalidate_delivery_candidate(
    conn,
    delivery_human_id: str,
    *,
    reason: str,
    rework_human_id: str,
    work_item_type: str,
    work_item_human_id: str,
    assignment: dict[str, Any] | None = None,
    base_git_sha: str | None = None,
    depend_on_human_ids: list[str] | None = None,
) -> InvalidateResult:
    delivery = get_job_by_human_id(conn, delivery_human_id)
    if delivery is None:
        return InvalidateResult(
            delivery_human_id=delivery_human_id,
            invalidated=False,
            error=f"Job {delivery_human_id} not found",
        )
    if delivery.queue != "DELIVERY":
        return InvalidateResult(
            delivery_human_id=delivery_human_id,
            invalidated=False,
            error=f"{delivery_human_id} is not a DELIVERY job",
        )

    existing_rework = get_job_by_human_id(conn, rework_human_id)
    if existing_rework is not None:
        if delivery.outcome != "INVALIDATED":
            set_job_outcome(
                conn,
                delivery.id,
                outcome="INVALIDATED",
                superseded_by_job_id=existing_rework.id,
            )
        return InvalidateResult(
            delivery_human_id=delivery_human_id,
            invalidated=True,
            rework_human_id=rework_human_id,
            assurance_cancelled=[],
        )

    cancelled = _cancel_assurance_for_delivery(
        conn,
        delivery,
        reason=f"Invalidated delivery candidate: {reason}",
    )

    identity = None
    if delivery.identity_snapshot_json:
        try:
            identity = json.loads(delivery.identity_snapshot_json)
        except json.JSONDecodeError:
            identity = None
    if identity is None:
        identity = {
            "project_human_id": delivery.project_human_id,
            "repository_root": delivery.repository_root,
        }

    rework = create_job(
        conn,
        human_id=rework_human_id,
        project_human_id=delivery.project_human_id,
        repository_root=delivery.repository_root,
        agent_role="DELIVERY",
        queue="DELIVERY",
        status="READY",
        iteration_human_id=delivery.iteration_human_id,
        work_item_type=work_item_type,
        work_item_human_id=work_item_human_id,
        requires_worktree=True,
        worktree_name=f"{delivery.project_human_id}__{rework_human_id}",
        base_git_sha=base_git_sha or delivery.base_git_sha,
        identity_snapshot=identity,
        assignment=assignment,
    )

    for dep_hid in depend_on_human_ids or []:
        dep = get_job_by_human_id(conn, dep_hid)
        if dep is None:
            raise OrchestrationError(f"Missing dependency job {dep_hid}")
        add_job_dependency(conn, rework.id, dep.id)

    set_job_outcome(
        conn,
        delivery.id,
        outcome="INVALIDATED",
        superseded_by_job_id=rework.id,
    )
    insert_candidate_invalidation(
        conn,
        delivery_job_id=delivery.id,
        invalidated_candidate_sha=delivery.candidate_git_sha,
        reason=reason,
        rework_job_id=rework.id,
    )
    append_run_event(
        conn,
        delivery.id,
        "delivery.candidate_invalidated",
        status="INVALIDATED",
        message=reason,
        payload={
            "invalidated_candidate_sha": delivery.candidate_git_sha,
            "rework_job": rework_human_id,
            "assurance_cancelled": cancelled,
        },
    )
    append_run_event(
        conn,
        rework.id,
        "delivery.rework_created",
        status="READY",
        message=f"Rework for invalidated {delivery_human_id}",
        payload={
            "supersedes": delivery_human_id,
            "work_item_type": work_item_type,
            "work_item_human_id": work_item_human_id,
        },
    )
    return InvalidateResult(
        delivery_human_id=delivery_human_id,
        invalidated=True,
        assurance_cancelled=cancelled,
        rework_human_id=rework_human_id,
    )


def rewire_integration_dependencies(
    conn,
    *,
    integration_human_id: str,
    replace_deps: dict[str, str],
) -> bool:
    """Replace INTEGRATION depends_on edges: old_delivery -> rework."""
    integ = get_job_by_human_id(conn, integration_human_id)
    if integ is None:
        return False
    for old_hid, new_hid in replace_deps.items():
        old = get_job_by_human_id(conn, old_hid)
        new = get_job_by_human_id(conn, new_hid)
        if old is None or new is None:
            raise OrchestrationError(
                f"Cannot rewire {integration_human_id}: missing {old_hid} or {new_hid}"
            )
        conn.execute(
            """
            DELETE FROM orchestration_job_dependencies
            WHERE job_id = ? AND depends_on_job_id = ?
            """,
            (integ.id, old.id),
        )
        existing = conn.execute(
            """
            SELECT 1 FROM orchestration_job_dependencies
            WHERE job_id = ? AND depends_on_job_id = ?
            """,
            (integ.id, new.id),
        ).fetchone()
        if existing is None:
            add_job_dependency(conn, integ.id, new.id)
    append_run_event(
        conn,
        integ.id,
        "integration.deps_rewired",
        status=integ.status,
        message="Rewired delivery dependencies onto rework jobs",
        payload={"replace": replace_deps},
    )
    return True


def _find_or_create_story(
    *,
    repository_root: Path,
    python_executable: Path | None,
    title: str,
    description: str,
    known: dict[str, set[str]],
) -> str:
    for hid in sorted(known.get("story", set())):
        shown = show_work_item(
            repository_root,
            "story",
            hid,
            python_executable=python_executable,
        )
        if shown and shown.get("title") == title:
            return hid
    result = create_projectctl_entity(
        repository_root,
        "story",
        title=title,
        description=description,
        python_executable=python_executable,
    )
    for line in (result.stdout or "").splitlines():
        if line.startswith("Created "):
            return line.split()[1]
    refreshed = read_work_item_ids(
        repository_root, python_executable=python_executable
    )
    created = refreshed.get("story", set()) - known.get("story", set())
    if len(created) == 1:
        return next(iter(created))
    raise OrchestrationError(
        f"Could not determine created story id for {title!r}: {result.stdout}"
    )


def reconcile_prj003_iter002_fat(
    *,
    db_path: Path | str | None = None,
    registry_path: Path | str | None = None,
    projectctl_runner=None,
    ensure_work_items: bool = True,
    work_item_map: dict[str, str] | None = None,
) -> FatReconcileResult:
    """Governed FAT reconciliation for PRJ-003 ITER-002 no-op deliveries.

    Does not dispatch workers. Preserves historical SUCCEEDED events.
    """
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    reg = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    initialize_database(path)
    project_id = "PRJ-003"
    iteration_id = "ITER-002"
    result = FatReconcileResult(
        project_human_id=project_id, iteration_human_id=iteration_id
    )

    if work_item_map:
        result.work_items.update(work_item_map)

    validated = resolve_validated_repo(
        project_id, registry_path=reg, projectctl_runner=projectctl_runner
    )
    if ensure_work_items and not work_item_map:
        ensure_iteration(
            validated.git_root,
            iteration_id,
            name="Iteration 2 — due date, priority, overdue",
            python_executable=validated.projectctl_python,
        )
        known = read_work_item_ids(
            validated.git_root, python_executable=validated.projectctl_python
        )
        for spec in _ITER002_STORIES:
            hid = _find_or_create_story(
                repository_root=validated.git_root,
                python_executable=validated.projectctl_python,
                title=spec["title"],
                description=spec["description"],
                known=known,
            )
            result.work_items[spec["key"]] = hid
            known.setdefault("story", set()).add(hid)
            result.messages.append(f"work_item {spec['key']} -> story {hid}")

    with connection(path) as conn:
        replace: dict[str, str] = {}
        for spec in _ITER002_STORIES:
            story_id = result.work_items.get(spec["key"])
            if not story_id:
                raise OrchestrationError(
                    f"Missing work item mapping for {spec['key']}"
                )
            shown = None
            if ensure_work_items and not work_item_map:
                shown = show_work_item(
                    validated.git_root,
                    "story",
                    story_id,
                    python_executable=validated.projectctl_python,
                )
            assignment = {
                "requirement_ref": f"story:{story_id}",
                "title": (shown or {}).get("title") or spec["title"],
                "acceptance_criteria": _extract_acs(
                    (shown or {}).get("description") or spec["description"]
                ),
                "scope_summary": spec["title"],
                "definition_of_ready": [
                    "Authoritative story exists in projectctl",
                    "Architecture job SUCCEEDED for iteration",
                    "Exact base SHA known",
                ],
                "definition_of_done": [
                    "Committed candidate SHA differs from base",
                    "Acceptance criteria addressed in candidate",
                    "Independent QA handoff only after valid candidate",
                ],
                "expected_implementation_evidence": [
                    "Committed git revision in isolated worktree",
                    "candidate_git_sha != base_git_sha",
                ],
            }
            inv = invalidate_delivery_candidate(
                conn,
                spec["old_job"],
                reason=(
                    "FAT Step 29: DELIVERY marked SUCCEEDED with no-op candidate "
                    f"(base==candidate) and missing work-item context; "
                    f"superseded by {spec['rework_job']}"
                ),
                rework_human_id=spec["rework_job"],
                work_item_type="story",
                work_item_human_id=story_id,
                assignment=assignment,
                base_git_sha="56d580d2eca1a634a86990241d4da2958c3323ff",
                depend_on_human_ids=["JOB-P2-ARCH"],
            )
            # Force iteration id on rework
            if inv.rework_human_id:
                rework = get_job_by_human_id(conn, inv.rework_human_id)
                if rework:
                    conn.execute(
                        """
                        UPDATE orchestration_jobs
                        SET iteration_human_id = ?
                        WHERE id = ?
                        """,
                        (iteration_id, rework.id),
                    )
                replace[spec["old_job"]] = inv.rework_human_id
            result.invalidations.append(inv)

        if replace:
            result.integration_rewired = rewire_integration_dependencies(
                conn,
                integration_human_id="JOB-P2-INTEGRATION",
                replace_deps=replace,
            )
            result.messages.append(
                "Rewired JOB-P2-INTEGRATION dependencies onto rework jobs"
            )

        integ = get_job_by_human_id(conn, "JOB-P2-INTEGRATION")
        rel = get_job_by_human_id(conn, "JOB-P2-RELEASE")
        if integ and rel:
            result.messages.append(
                f"JOB-P2-INTEGRATION status={integ.status}; "
                f"JOB-P2-RELEASE status={rel.status}"
            )

    return result
