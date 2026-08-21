"""Role-specific Cursor Agent prompt construction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from projectos.errors import OrchestrationError
from projectos.projectctl_bridge import show_work_item
from projectos.store import OrchestrationJob

ROLE_INSTRUCTIONS: dict[str, str] = {
    "PM": (
        "You are the Project Manager agent. Coordinate work using authoritative "
        "project-control state. Do not implement delivery code changes. Do not "
        "approve QA or release."
    ),
    "ARCHITECTURE": (
        "You are the Architecture agent. Propose or apply architecture changes "
        "within governance constraints. Do not self-approve QA or release."
    ),
    "DELIVERY": (
        "You are the Delivery agent. Implement the assigned work item in the "
        "provided worktree. Commit coherent candidate changes before finishing. "
        "Do not self-approve QA or release. If no code change is legitimately "
        "required, end with a line exactly: OUTCOME: NO_CHANGE and explain why."
    ),
    "ASSURANCE_FUNCTIONAL": (
        "You are Functional Assurance. Validate the candidate revision against "
        "acceptance criteria. Independent QA only — do not modify delivery code "
        "to make tests pass silently."
    ),
    "ASSURANCE_INTEGRATION": (
        "You are Integration Assurance. Validate integration behavior for the "
        "candidate revision. Independent QA only."
    ),
    "ASSURANCE_SECURITY": (
        "You are Security Assurance. Review the candidate revision for security "
        "issues. Independent QA only."
    ),
    "ASSURANCE_QUALITY": (
        "You are Quality Assurance. Assess overall quality for the candidate "
        "revision. Independent QA only."
    ),
    "RELEASE": (
        "You are the Release agent. Evaluate release readiness using governed "
        "gates. Do not treat worker SUCCEEDED as release approval by itself."
    ),
}


@dataclass(frozen=True)
class ResolvedAssignment:
    work_item_type: str | None
    work_item_human_id: str | None
    title: str | None
    acceptance_criteria: list[str]
    requirement_ref: str | None
    dependencies: list[str]
    architecture_refs: list[str]
    definition_of_ready: list[str]
    definition_of_done: list[str]
    expected_implementation_evidence: list[str]
    raw: dict[str, Any]


def _parse_assignment_json(job: OrchestrationJob) -> dict[str, Any]:
    if not job.assignment_json:
        return {}
    try:
        data = json.loads(job.assignment_json)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _acs_from_description(description: str | None) -> list[str]:
    lines: list[str] = []
    for line in (description or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("- AC-") or stripped.startswith("AC-"):
            lines.append(stripped.lstrip("- ").strip())
    return lines


def resolve_delivery_assignment(
    job: OrchestrationJob,
    *,
    repository_root: Path,
    python_executable: Path | None = None,
) -> ResolvedAssignment:
    """Resolve authoritative work-item context for a DELIVERY job."""
    assignment = _parse_assignment_json(job)
    title = assignment.get("title")
    acs = list(assignment.get("acceptance_criteria") or [])
    requirement_ref = assignment.get("requirement_ref")
    wi_type = job.work_item_type
    wi_id = job.work_item_human_id

    if wi_type and wi_id:
        shown = show_work_item(
            repository_root,
            str(wi_type),
            str(wi_id),
            python_executable=python_executable,
        )
        if shown is not None:
            title = title or shown.get("title")
            if not acs:
                acs = _acs_from_description(shown.get("description"))
            requirement_ref = requirement_ref or f"{wi_type}:{wi_id}"
        elif not (requirement_ref and acs):
            raise OrchestrationError(
                f"Cannot resolve work item {wi_type} {wi_id} from projectctl "
                f"in {repository_root}"
            )
        else:
            requirement_ref = requirement_ref or f"{wi_type}:{wi_id}"
    elif requirement_ref and acs:
        title = title or str(requirement_ref)
    else:
        raise OrchestrationError(
            f"DELIVERY job {job.human_id} lacks resolvable work-item context "
            "(need work_item_type+work_item_human_id or requirement_ref+"
            "acceptance_criteria)"
        )

    if not acs:
        raise OrchestrationError(
            f"DELIVERY job {job.human_id} resolved {wi_type} {wi_id} but "
            "acceptance criteria are empty"
        )

    return ResolvedAssignment(
        work_item_type=wi_type,
        work_item_human_id=wi_id,
        title=str(title) if title else None,
        acceptance_criteria=[str(a) for a in acs],
        requirement_ref=str(requirement_ref) if requirement_ref else None,
        dependencies=[str(d) for d in (assignment.get("dependencies") or [])],
        architecture_refs=[
            str(a) for a in (assignment.get("architecture_refs") or [])
        ],
        definition_of_ready=[
            str(d) for d in (assignment.get("definition_of_ready") or [])
        ],
        definition_of_done=[
            str(d) for d in (assignment.get("definition_of_done") or [])
        ],
        expected_implementation_evidence=[
            str(e)
            for e in (assignment.get("expected_implementation_evidence") or [])
        ],
        raw=assignment,
    )


def build_role_prompt(
    job: OrchestrationJob,
    *,
    workspace_path: str,
    base_git_sha: str | None = None,
    extra_context: str | None = None,
    resolved: ResolvedAssignment | None = None,
) -> str:
    role = job.agent_role.upper()
    instruction = ROLE_INSTRUCTIONS.get(
        role,
        (
            f"You are the {job.agent_role} agent for ProjectOS. Follow project "
            "governance. Worker success is not QA or release acceptance."
        ),
    )
    lines = [
        instruction,
        "",
        "## Authoritative job context",
        f"- job_human_id: {job.human_id}",
        f"- project_human_id: {job.project_human_id}",
        f"- repository_root: {job.repository_root}",
        f"- workspace: {workspace_path}",
        f"- queue: {job.queue}",
        f"- agent_role: {job.agent_role}",
        f"- attempt: {job.attempt + 1} / {job.max_attempts}",
    ]
    if job.iteration_human_id:
        lines.append(f"- iteration_human_id: {job.iteration_human_id}")
    if base_git_sha or job.base_git_sha:
        lines.append(f"- base_git_sha: {base_git_sha or job.base_git_sha}")
    if job.candidate_git_sha:
        lines.append(f"- candidate_git_sha: {job.candidate_git_sha}")

    if resolved is not None:
        lines.extend(
            [
                "",
                "## Assigned work item (authoritative)",
                f"- work_item_id: {resolved.work_item_type or 'ref'} "
                f"{resolved.work_item_human_id or resolved.requirement_ref or ''}".rstrip(),
                f"- title: {resolved.title or '(untitled)'}",
            ]
        )
        if resolved.requirement_ref:
            lines.append(f"- requirement_ref: {resolved.requirement_ref}")
        lines.append("- acceptance_criteria:")
        for ac in resolved.acceptance_criteria:
            lines.append(f"  - {ac}")
        if resolved.dependencies:
            lines.append("- dependencies:")
            for dep in resolved.dependencies:
                lines.append(f"  - {dep}")
        if resolved.architecture_refs:
            lines.append("- architecture/ADR refs:")
            for ref in resolved.architecture_refs:
                lines.append(f"  - {ref}")
        if resolved.definition_of_ready:
            lines.append("- Definition of Ready:")
            for item in resolved.definition_of_ready:
                lines.append(f"  - {item}")
        if resolved.definition_of_done:
            lines.append("- Definition of Done:")
            for item in resolved.definition_of_done:
                lines.append(f"  - {item}")
        if resolved.expected_implementation_evidence:
            lines.append("- expected implementation evidence:")
            for item in resolved.expected_implementation_evidence:
                lines.append(f"  - {item}")
    elif job.work_item_type or job.work_item_human_id:
        lines.append(
            f"- work_item: {job.work_item_type or 'unknown'} "
            f"{job.work_item_human_id or ''}".rstrip()
        )

    lines.extend(
        [
            "",
            "## Governance",
            "- Use project-control / projectctl state in this repository as source of truth.",
            "- Preserve Phase 1 isolation: one project per repository.",
            "- Independent QA and release gates remain separate from worker SUCCEEDED.",
            "- Do not silently bypass assurance or release policy.",
            "- Do not infer feature scope from the job human_id alone.",
        ]
    )
    if extra_context:
        lines.extend(["", "## Additional context", extra_context.strip()])
    lines.extend(
        [
            "",
            "## Required outcome",
            "Complete only this worker task. Report concrete results, risks, and "
            "any follow-up defects without claiming QA/release approval.",
        ]
    )
    return "\n".join(lines) + "\n"
