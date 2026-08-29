"""Governed Phase 2 release-readiness evaluation.

The RELEASE worker evaluates an immutable integrated candidate using:
- ProjectOS jobs + qa_evidence
- projectctl state from the registered delivery repository (never a worktree DB)

Evidence is written only under ProjectOS run logs. The product worktree is not
modified. Worker SUCCEEDED means evaluation finished; it is not release approval.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from projectos.errors import OrchestrationError, ProjectctlError
from projectos.paths import RUN_OUTPUT_DIR
from projectos.projectctl_bridge import (
    ProjectctlResult,
    list_entity_ids,
    resolve_validated_repo,
    run_projectctl,
)
from projectos.qa_handoff import REQUIRED_ASSURANCE
from projectos.store import OrchestrationJob, list_jobs_for_project
from projectos.worktree import current_head_sha, is_dirty

GATE_READY_OUTCOME = "GATE_READY"
GATE_REJECTED_OUTCOME = "GATE_REJECTED"
DEFAULT_RELEASE_NAME = "ITER-002 Phase 2 increment"
DEFAULT_RELEASE_VERSION = "0.2.0"
ITERATION_SYNC_TARGET = "release_candidate"

ACCEPTED_STORY_SHAS = {
    "US-007": "b69ac0720f43d4d0bec3427a098d80938959ae2d",
    "US-008": "8f45ef82936abb82f00b5d2ec25ef8b2d07638e1",
}

_ITERATION_PROGRESSION = (
    "planned",
    "in_delivery",
    "in_qa",
    "integrating",
    "release_candidate",
)


class ProjectctlReleaseOps(Protocol):
    def resolve_db(self, repository_root: Path) -> Path: ...

    def run(
        self,
        repository_root: Path,
        args: list[str],
        *,
        db_path: Path | None = None,
    ) -> ProjectctlResult: ...


@dataclass
class DefaultProjectctlOps:
    python_executable: Path | None = None

    def resolve_db(self, repository_root: Path) -> Path:
        return resolve_authoritative_projectctl_db(repository_root)

    def run(
        self,
        repository_root: Path,
        args: list[str],
        *,
        db_path: Path | None = None,
    ) -> ProjectctlResult:
        return run_projectctl(
            repository_root,
            args,
            python_executable=self.python_executable,
            db_path=db_path,
            require_zero=True,
        )


@dataclass
class StoryLineage:
    work_item_human_id: str
    delivery_human_id: str
    candidate_git_sha: str
    assurance: dict[str, str] = field(default_factory=dict)
    qa_manager_human_id: str | None = None
    qa_manager_status: str | None = None


@dataclass
class ReleaseEvaluation:
    approved: bool
    reasons: list[str]
    candidate_sha: str
    evidence_dir: Path
    readiness_report_path: Path
    qa_package_path: Path | None
    release_human_id: str | None
    release_status: str | None
    iteration_status: str | None
    workspace_clean: bool
    workspace_head: str | None
    outcome: str
    pm_job: str | None = None
    architecture_job: str | None = None
    integration_job: str | None = None
    stories: list[StoryLineage] = field(default_factory=list)


def resolve_authoritative_projectctl_db(
    repository_root: Path | None = None,
    *,
    project_human_id: str | None = None,
    registry_path: Path | str | None = None,
    project_context=None,
    projectctl_runner=None,
) -> Path:
    from projectos.project_context import ProjectContext, resolve_project_context

    ctx = project_context
    if ctx is None and project_human_id:
        ctx = resolve_project_context(
            project_human_id,
            registry_path=registry_path,
            claimed_repository_root=repository_root,
            projectctl_runner=projectctl_runner,
        )
    elif ctx is not None and repository_root is not None:
        ctx.assert_repository_root(repository_root)

    if ctx is not None:
        db = ctx.projectctl_db_path
    elif repository_root is not None:
        root = Path(repository_root).resolve()
        db = root / "project-control" / "project.db"
    else:
        raise ProjectctlError(
            "project_human_id or repository_root is required to resolve project.db"
        )
    if not db.is_file():
        raise ProjectctlError(
            "authoritative project.db missing at registered delivery repository "
            f"{db}. Release must not invent a worktree database."
        )
    if db.stat().st_size == 0:
        raise ProjectctlError(
            f"authoritative project.db is empty/unusable at {db}"
        )
    return db


def evidence_dir_for_job(job_human_id: str, *, run_root: Path | None = None) -> Path:
    root = Path(run_root) if run_root is not None else RUN_OUTPUT_DIR
    path = root / job_human_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def iteration_steps(current: str, target: str) -> list[str]:
    cur = current.strip().lower()
    tgt = target.strip().lower()
    if cur == tgt:
        return []
    if cur not in _ITERATION_PROGRESSION or tgt not in _ITERATION_PROGRESSION:
        raise OrchestrationError(
            f"cannot advance iteration from {current!r} to {target!r}"
        )
    start = _ITERATION_PROGRESSION.index(cur)
    end = _ITERATION_PROGRESSION.index(tgt)
    if end <= start:
        raise OrchestrationError(
            f"iteration status {current} is not before {target}"
        )
    return list(_ITERATION_PROGRESSION[start + 1 : end + 1])


def _ok(job: OrchestrationJob) -> bool:
    return job.status == "SUCCEEDED" and job.outcome not in {
        "INVALIDATED",
        "SUPERSEDED",
        "NO_CHANGE",
    }


def _find_queue(jobs: list[OrchestrationJob], queue: str) -> OrchestrationJob | None:
    hits = [j for j in jobs if j.queue == queue and _ok(j)]
    return hits[-1] if hits else None


def _find_story_delivery(
    jobs: list[OrchestrationJob], story_id: str
) -> OrchestrationJob | None:
    hits = [
        j
        for j in jobs
        if j.queue == "DELIVERY"
        and j.work_item_human_id == story_id
        and _ok(j)
        and j.candidate_git_sha
    ]
    return hits[-1] if hits else None


def _qa_map(conn, delivery_id: int, sha: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT assurance_role, result FROM qa_evidence
        WHERE delivery_job_id = ? AND candidate_git_sha = ?
        """,
        (delivery_id, sha),
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def _field(stdout: str, key: str) -> str | None:
    needle = f"{key}:"
    for line in stdout.splitlines():
        if line.lower().startswith(needle.lower()):
            return line.split(":", 1)[1].strip() or None
    return None


def assemble_qa_package(
    conn,
    job: OrchestrationJob,
    *,
    expected_integration_sha: str,
    evidence_dir: Path,
    required_story_shas: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str], list[StoryLineage]]:
    reasons: list[str] = []
    jobs = list_jobs_for_project(conn, job.project_human_id)
    stories: list[StoryLineage] = []

    pm = _find_queue(jobs, "PM")
    arch = _find_queue(jobs, "ARCHITECTURE")
    integ = _find_queue(jobs, "INTEGRATION")
    if pm is None:
        reasons.append("PM job has not SUCCEEDED")
    if arch is None:
        reasons.append("ARCHITECTURE job has not SUCCEEDED")
    if integ is None:
        reasons.append("INTEGRATION job has not SUCCEEDED")
    elif integ.candidate_git_sha != expected_integration_sha:
        reasons.append(
            "INTEGRATION candidate "
            f"{integ.candidate_git_sha} != required {expected_integration_sha}"
        )

    story_ids = list(required_story_shas) if required_story_shas else ["US-007", "US-008"]
    for story_id in story_ids:
        delivery = _find_story_delivery(jobs, story_id)
        if delivery is None:
            reasons.append(f"no SUCCEEDED DELIVERY for {story_id}")
            continue
        sha = delivery.candidate_git_sha or ""
        expected_sha = (required_story_shas or {}).get(story_id)
        if expected_sha and sha != expected_sha:
            reasons.append(
                f"{story_id} candidate {sha} != accepted lineage {expected_sha}"
            )
        qa = _qa_map(conn, delivery.id, sha)
        for role in REQUIRED_ASSURANCE:
            if qa.get(role) != "pass":
                reasons.append(
                    f"{story_id} missing passing {role} evidence for {sha}"
                )
        mgr = next(
            (j for j in jobs if j.human_id == f"{delivery.human_id}__QA_MANAGER"),
            None,
        )
        if mgr is None or mgr.status != "SUCCEEDED":
            reasons.append(f"{story_id} QA Manager has not SUCCEEDED")
        stories.append(
            StoryLineage(
                work_item_human_id=story_id,
                delivery_human_id=delivery.human_id,
                candidate_git_sha=sha,
                assurance=qa,
                qa_manager_human_id=mgr.human_id if mgr else None,
                qa_manager_status=mgr.status if mgr else None,
            )
        )

    package = {
        "project_human_id": job.project_human_id,
        "iteration_human_id": job.iteration_human_id,
        "integration_sha": expected_integration_sha,
        "pm_job": pm.human_id if pm else None,
        "architecture_job": arch.human_id if arch else None,
        "integration_job": integ.human_id if integ else None,
        "stories": [
            {
                "work_item_human_id": s.work_item_human_id,
                "delivery_human_id": s.delivery_human_id,
                "candidate_git_sha": s.candidate_git_sha,
                "assurance": s.assurance,
                "qa_manager_human_id": s.qa_manager_human_id,
                "qa_manager_status": s.qa_manager_status,
            }
            for s in stories
        ],
        "worker_succeeded_is_not_release_approval": True,
    }
    (evidence_dir / "qa-package.json").write_text(
        json.dumps(package, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# ITER-002 QA package (derived from ProjectOS evidence)",
        "",
        f"- project: {job.project_human_id}",
        f"- iteration: {job.iteration_human_id}",
        f"- integration SHA: {expected_integration_sha}",
        f"- PM: {package['pm_job']}",
        f"- Architecture: {package['architecture_job']}",
        f"- Integration: {package['integration_job']}",
        "",
        "Assembled from persisted ProjectOS jobs and qa_evidence.",
        "Worker SUCCEEDED is not release approval.",
        "",
    ]
    for s in stories:
        lines.append(f"## {s.work_item_human_id} ({s.delivery_human_id})")
        lines.append(f"- candidate: {s.candidate_git_sha}")
        for role, result in sorted(s.assurance.items()):
            lines.append(f"- {role}: {result}")
        lines.append(
            f"- QA Manager: {s.qa_manager_human_id} {s.qa_manager_status}"
        )
        lines.append("")
    (evidence_dir / "qa-package.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return package, reasons, stories


def _ensure_rel002(
    *,
    ops: ProjectctlReleaseOps,
    repository_root: Path,
    db_path: Path,
    iteration_human_id: str,
    candidate_sha: str,
    git_cwd: Path,
    qa_package_path: Path,
) -> tuple[str | None, str | None, list[str]]:
    reasons: list[str] = []
    ids = list_entity_ids(
        ops.run(repository_root, ["release", "list"], db_path=db_path).stdout
    )
    rel_id = "REL-002" if "REL-002" in ids else None
    if rel_id is None:
        created = ops.run(
            repository_root,
            [
                "release",
                "create",
                "--name",
                DEFAULT_RELEASE_NAME,
                "--version",
                DEFAULT_RELEASE_VERSION,
                "--iteration",
                iteration_human_id,
            ],
            db_path=db_path,
        )
        for token in created.stdout.replace(":", " ").split():
            if token.startswith("REL-"):
                rel_id = token
                break
        if rel_id is None:
            ids = list_entity_ids(
                ops.run(
                    repository_root, ["release", "list"], db_path=db_path
                ).stdout
            )
            rel_id = next((i for i in ids if i != "REL-001"), None)
        if rel_id is None:
            reasons.append("failed to create REL-002 via projectctl")
            return None, None, reasons

    shown = ops.run(
        repository_root, ["release", "show", rel_id], db_path=db_path
    )
    status = (_field(shown.stdout, "status") or "").lower()
    if status in {"", "planned"}:
        ops.run(
            repository_root,
            [
                "release",
                "status",
                rel_id,
                "candidate",
                "--git-sha",
                candidate_sha,
                "--iteration",
                iteration_human_id,
                "--git-cwd",
                str(git_cwd),
                "--reason",
                "Phase 2 integration candidate bound from ProjectOS evidence",
            ],
            db_path=db_path,
        )
        status = "candidate"
    shown = ops.run(
        repository_root, ["release", "show", rel_id], db_path=db_path
    )
    git_sha = _field(shown.stdout, "git_sha")
    if git_sha and git_sha.lower() != candidate_sha.lower():
        reasons.append(
            f"{rel_id} git_sha {git_sha} != integrated candidate {candidate_sha}"
        )
    status = (_field(shown.stdout, "status") or status).lower()
    if status == "candidate":
        ops.run(
            repository_root,
            [
                "release",
                "status",
                rel_id,
                "qa_passed",
                "--qa-evidence",
                str(qa_package_path),
                "--reason",
                "QA package assembled from ProjectOS assurance evidence",
            ],
            db_path=db_path,
        )
        status = "qa_passed"
    if status not in {"candidate", "qa_passed"}:
        reasons.append(
            f"{rel_id} is {status or 'unknown'}; expected candidate or qa_passed "
            "before release complete"
        )
    if status == "released":
        reasons.append(
            f"{rel_id} is already released; worker evaluation must not re-complete it"
        )
    return rel_id, status, reasons


def _sync_iteration(
    *,
    ops: ProjectctlReleaseOps,
    repository_root: Path,
    db_path: Path,
    iteration_human_id: str,
    target: str,
) -> tuple[str | None, list[str]]:
    reasons: list[str] = []
    shown = ops.run(
        repository_root,
        ["iteration", "show", iteration_human_id],
        db_path=db_path,
    )
    current = (_field(shown.stdout, "status") or "planned").lower()
    if current == target:
        return current, reasons
    try:
        steps = iteration_steps(current, target)
    except OrchestrationError as exc:
        reasons.append(f"iteration {iteration_human_id}: {exc}")
        return current, reasons
    for step in steps:
        ops.run(
            repository_root,
            [
                "iteration",
                "status",
                iteration_human_id,
                step,
                "--reason",
                "ProjectOS evidence-driven iteration advancement",
            ],
            db_path=db_path,
        )
        current = step
    return current, reasons


def evaluate_release_job(
    conn,
    job: OrchestrationJob,
    *,
    workspace: Path,
    registry_path: Path | None = None,
    evidence_root: Path | None = None,
    ops: ProjectctlReleaseOps | None = None,
    expected_integration_sha: str | None = None,
    required_story_shas: dict[str, str] | None = None,
) -> ReleaseEvaluation:
    """Evaluate RELEASE against persisted evidence. Does not write into workspace."""
    sha = expected_integration_sha or job.source_candidate_sha or ""
    ev_dir = evidence_dir_for_job(job.human_id, run_root=evidence_root)
    reasons: list[str] = []
    head: str | None = None
    clean = False
    try:
        head = current_head_sha(workspace)
        clean = not is_dirty(workspace)
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"cannot inspect evaluation workspace: {exc}")

    if not sha:
        reasons.append("RELEASE job missing source_candidate_sha")
    if head and sha and head.lower() != sha.lower():
        reasons.append(
            f"evaluation workspace HEAD {head} != integrated candidate {sha}"
        )
    if not clean:
        reasons.append(
            "evaluation workspace is dirty; release must observe an immutable candidate"
        )

    story_shas = required_story_shas
    if (
        story_shas is None
        and job.project_human_id == "PRJ-003"
        and job.iteration_human_id == "ITER-002"
    ):
        story_shas = ACCEPTED_STORY_SHAS
    package, qa_reasons, stories = assemble_qa_package(
        conn,
        job,
        expected_integration_sha=sha,
        evidence_dir=ev_dir,
        required_story_shas=story_shas,
    )
    reasons.extend(qa_reasons)
    qa_md = ev_dir / "qa-package.md"

    rel_id = None
    rel_status = None
    iter_status = None
    ctl = ops or DefaultProjectctlOps()
    try:
        if ops is None:
            validated = resolve_validated_repo(
                job.project_human_id, registry_path=registry_path
            )
            registered_root = Path(validated.git_root).resolve()
            if Path(job.repository_root).resolve() != registered_root:
                reasons.append(
                    "repository identity mismatch: job repository_root "
                    f"{job.repository_root} != registered {registered_root}"
                )
        else:
            registered_root = Path(job.repository_root).resolve()
        db_path = ctl.resolve_db(registered_root)
        if not reasons:
            rel_id, rel_status, rel_reasons = _ensure_rel002(
                ops=ctl,
                repository_root=registered_root,
                db_path=db_path,
                iteration_human_id=job.iteration_human_id or "ITER-002",
                candidate_sha=sha,
                git_cwd=workspace,
                qa_package_path=qa_md,
            )
            reasons.extend(rel_reasons)
            iter_status, iter_reasons = _sync_iteration(
                ops=ctl,
                repository_root=registered_root,
                db_path=db_path,
                iteration_human_id=job.iteration_human_id or "ITER-002",
                target=ITERATION_SYNC_TARGET,
            )
            reasons.extend(iter_reasons)
    except (ProjectctlError, OrchestrationError, FileNotFoundError) as exc:
        reasons.append(f"projectctl context: {exc}")

    try:
        head_after = current_head_sha(workspace)
        clean_after = not is_dirty(workspace)
        if head and head_after != head:
            reasons.append("evaluation mutated candidate HEAD")
            clean = False
        if not clean_after:
            reasons.append("evaluation dirtied the product candidate workspace")
            clean = False
        else:
            clean = clean and clean_after
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"post-evaluation workspace check failed: {exc}")

    approved = not reasons
    report_path = ev_dir / "release-readiness.md"
    report = [
        "# Release readiness evaluation",
        "",
        f"- job: {job.human_id}",
        f"- project: {job.project_human_id}",
        f"- iteration: {job.iteration_human_id}",
        f"- candidate SHA: {sha}",
        f"- workspace HEAD: {head}",
        f"- workspace clean: {clean}",
        f"- REL: {rel_id} ({rel_status})",
        f"- iteration status: {iter_status}",
        f"- gate: {'READY' if approved else 'REJECTED'}",
        "",
        "Worker SUCCEEDED is not release approval.",
        "`projectctl release complete` is the only transition to released.",
        "",
        "## Reasons" if reasons else "## Result",
    ]
    if reasons:
        report.extend(f"- {r}" for r in reasons)
    else:
        report.append("- All required ProjectOS/projectctl evidence is present.")
        report.append("- REL-002 is at qa_passed/candidate; not completed.")
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    (ev_dir / "release-readiness.json").write_text(
        json.dumps(
            {
                "approved": approved,
                "reasons": reasons,
                "candidate_sha": sha,
                "release_human_id": rel_id,
                "release_status": rel_status,
                "iteration_status": iter_status,
                "workspace_clean": clean,
                "qa_package": package,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return ReleaseEvaluation(
        approved=approved,
        reasons=reasons,
        candidate_sha=sha,
        evidence_dir=ev_dir,
        readiness_report_path=report_path,
        qa_package_path=qa_md if qa_md.is_file() else None,
        release_human_id=rel_id,
        release_status=rel_status,
        iteration_status=iter_status,
        workspace_clean=clean,
        workspace_head=head,
        outcome=GATE_READY_OUTCOME if approved else GATE_REJECTED_OUTCOME,
        pm_job=package.get("pm_job"),
        architecture_job=package.get("architecture_job"),
        integration_job=package.get("integration_job"),
        stories=stories,
    )
