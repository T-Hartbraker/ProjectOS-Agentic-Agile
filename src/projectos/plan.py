"""PM planning: schema-validated durable orchestration plans."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from projectos.constants import QUEUE_TO_ROLE, VALID_QUEUES
from projectos.cursor_adapter import CursorRunResult, invoke_cursor_agent
from projectos.db import connection
from projectos.errors import OrchestrationError, ProjectOSError
from projectos.migrate import initialize_database
from projectos.paths import DEFAULT_DB_PATH, DEFAULT_REGISTRY_PATH
from projectos.projectctl_bridge import read_work_item_ids, resolve_validated_repo
from projectos.store import (
    add_job_dependency,
    create_job,
    find_active_assignment,
    get_job_by_human_id,
)

PLAN_SCHEMA_VERSION = 1
_SPONSOR_OK = frozenset({"approved", "granted", "authorized", "sponsor-approved"})


def _assignment_from_plan_job(job: dict[str, Any]) -> dict[str, Any] | None:
    assignment: dict[str, Any] = {}
    for key in (
        "requirement_ref",
        "acceptance_criteria",
        "scope_summary",
        "title",
        "dependencies",
        "architecture_refs",
        "definition_of_ready",
        "definition_of_done",
        "expected_implementation_evidence",
    ):
        if key in job and job[key] is not None:
            assignment[key] = job[key]
    return assignment or None


@dataclass
class PlanResult:
    status: str
    project_human_id: str
    dry_run: bool
    jobs_created: list[str] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    error: str | None = None
    output_ref: str | None = None
    plan_source: str | None = None  # cursor | override | accepted_replay

    @property
    def ok(self) -> bool:
        return self.status in {"accepted", "dry_run"} and self.error is None


def extract_json_document(text: str) -> dict[str, Any]:
    """Extract a JSON object from model output (raw, fenced, or Cursor envelope)."""
    text = (text or "").strip()
    if not text:
        raise OrchestrationError("PM plan output was empty")

    def _coerce(data: Any) -> dict[str, Any] | None:
        if isinstance(data, dict):
            # Cursor JSON envelopes often nest the plan under result/message/content.
            if "schema_version" in data or "jobs" in data:
                return data
            for key in ("result", "message", "content", "plan", "output", "text"):
                nested = data.get(key)
                if isinstance(nested, dict) and (
                    "schema_version" in nested or "jobs" in nested
                ):
                    return nested
                if isinstance(nested, str) and nested.strip():
                    try:
                        return extract_json_document(nested)
                    except OrchestrationError:
                        continue
            return data
        if isinstance(data, str) and data.strip():
            try:
                return extract_json_document(data)
            except OrchestrationError:
                return None
        return None

    try:
        parsed = json.loads(text)
        coerced = _coerce(parsed)
        if coerced is not None:
            return coerced
    except json.JSONDecodeError:
        pass

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        data = json.loads(fence.group(1))
        coerced = _coerce(data)
        if coerced is not None:
            return coerced

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        data = json.loads(text[start : end + 1])
        coerced = _coerce(data)
        if coerced is not None:
            return coerced

    raise OrchestrationError("PM plan output did not contain a JSON object")


def plan_text_from_cursor_result(cursor: CursorRunResult) -> str:
    """Prefer in-memory stdout; fall back to persisted stdout/stderr artifacts."""
    chunks: list[str] = []
    if cursor.stdout and cursor.stdout.strip():
        chunks.append(cursor.stdout)
    else:
        try:
            file_text = Path(cursor.stdout_ref).read_text(encoding="utf-8")
            if file_text.strip():
                chunks.append(file_text)
        except OSError:
            pass
    if cursor.stderr and cursor.stderr.strip():
        # Some agent modes emit the final JSON only on stderr.
        chunks.append(cursor.stderr)
    else:
        try:
            err_text = Path(cursor.stderr_ref).read_text(encoding="utf-8")
            if err_text.strip():
                chunks.append(err_text)
        except OSError:
            pass
    return "\n".join(chunks).strip()


def _ensure_release_depends_on_integration(jobs: list[Any]) -> list[str]:
    """Require every RELEASE job to depend on an INTEGRATION job in the plan.

    If a RELEASE job omits the edge and the plan has exactly one INTEGRATION
    job, that dependency is added deterministically.
    """
    errors: list[str] = []
    integration_ids = [
        str(job.get("human_id"))
        for job in jobs
        if isinstance(job, dict)
        and job.get("queue") == "INTEGRATION"
        and isinstance(job.get("human_id"), str)
    ]
    for job in jobs:
        if not isinstance(job, dict) or job.get("queue") != "RELEASE":
            continue
        hid = job.get("human_id")
        deps = [str(d) for d in (job.get("depends_on") or [])]
        if any(dep in integration_ids for dep in deps):
            job["depends_on"] = deps
            continue
        if len(integration_ids) == 1:
            deps.append(integration_ids[0])
            job["depends_on"] = deps
            continue
        if not integration_ids:
            errors.append(
                f"job {hid}: RELEASE requires a depends_on INTEGRATION job"
            )
        else:
            errors.append(
                f"job {hid}: RELEASE must depend_on an INTEGRATION job "
                f"(ambiguous candidates {integration_ids})"
            )
    return errors


def validate_plan_document(
    plan: dict[str, Any],
    *,
    expected_project_id: str,
    known_work_items: dict[str, set[str]] | None = None,
    existing_job_checker: Callable[[str, str, str], bool] | None = None,
) -> list[str]:
    """Return list of validation errors (empty => ok)."""
    errors: list[str] = []
    if int(plan.get("schema_version", -1)) != PLAN_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {PLAN_SCHEMA_VERSION} "
            f"(got {plan.get('schema_version')!r})"
        )
    if plan.get("project_human_id") != expected_project_id:
        errors.append(
            f"plan project_human_id {plan.get('project_human_id')!r} "
            f"!= requested {expected_project_id!r}"
        )
    sponsor = str(plan.get("sponsor_authority", "")).strip().lower()
    if sponsor not in _SPONSOR_OK:
        errors.append(
            f"Sponsor-authority violation: sponsor_authority={plan.get('sponsor_authority')!r}"
        )
    jobs = plan.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        errors.append("plan.jobs must be a non-empty array")
        return errors

    human_ids: set[str] = set()
    for index, job in enumerate(jobs):
        prefix = f"jobs[{index}]"
        if not isinstance(job, dict):
            errors.append(f"{prefix} must be an object")
            continue
        hid = job.get("human_id")
        if not isinstance(hid, str) or not hid.strip():
            errors.append(f"{prefix}.human_id required")
        else:
            if hid in human_ids:
                errors.append(f"duplicate plan job human_id {hid}")
            human_ids.add(hid)
        queue = job.get("queue")
        if queue not in VALID_QUEUES:
            errors.append(f"{prefix}.queue invalid: {queue!r}")
        role = job.get("agent_role") or QUEUE_TO_ROLE.get(str(queue), "")
        if not role:
            errors.append(f"{prefix}.agent_role missing")
        wi_type = job.get("work_item_type")
        wi_id = job.get("work_item_human_id")
        req_ref = job.get("requirement_ref")
        acs = job.get("acceptance_criteria")
        if queue == "DELIVERY":
            has_structured = bool(wi_type and wi_id)
            has_explicit = bool(
                req_ref
                and isinstance(acs, list)
                and any(str(a).strip() for a in acs)
            )
            if not has_structured and not has_explicit:
                errors.append(
                    f"{prefix}: DELIVERY jobs require work_item_type+"
                    "work_item_human_id (preferred) or requirement_ref+"
                    "acceptance_criteria"
                )
        if wi_id and known_work_items is not None and wi_type:
            bucket = known_work_items.get(str(wi_type), set())
            if bucket and str(wi_id) not in bucket:
                errors.append(f"unknown work item {wi_type} {wi_id}")
        if wi_id and queue and existing_job_checker is not None:
            if existing_job_checker(expected_project_id, str(wi_id), str(queue)):
                errors.append(
                    f"duplicate active assignment for {wi_id} on queue {queue}"
                )
        deps = job.get("depends_on") or []
        if not isinstance(deps, list):
            errors.append(f"{prefix}.depends_on must be an array")

    errors.extend(_ensure_release_depends_on_integration(jobs))

    by_id = {
        j.get("human_id"): j
        for j in jobs
        if isinstance(j, dict) and isinstance(j.get("human_id"), str)
    }
    for hid, job in by_id.items():
        for dep in job.get("depends_on") or []:
            if dep not in by_id:
                errors.append(f"job {hid} depends on unknown {dep}")
    indegree = {hid: 0 for hid in by_id}
    edges: dict[str, list[str]] = {hid: [] for hid in by_id}
    for hid, job in by_id.items():
        for dep in job.get("depends_on") or []:
            if dep in by_id:
                edges[dep].append(hid)
                indegree[hid] += 1
    queue_ids = [h for h, d in indegree.items() if d == 0]
    seen = 0
    while queue_ids:
        node = queue_ids.pop()
        seen += 1
        for nxt in edges[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue_ids.append(nxt)
    if by_id and seen != len(by_id):
        errors.append("malformed dependency graph: cycle detected")
    return errors


def _build_pm_prompt(
    project_human_id: str,
    iteration_human_id: str | None,
    *,
    work_request: dict[str, Any] | None = None,
) -> str:
    base = (
        "You are the Project Manager for ProjectOS. Produce ONLY a JSON object "
        f"matching schema_version={PLAN_SCHEMA_VERSION} for project "
        f"{project_human_id}. Include sponsor_authority, optional "
        "iteration_human_id, and jobs[] with human_id, queue, agent_role, "
        "work_item_type, work_item_human_id, depends_on, priority. "
        "Every DELIVERY job MUST include work_item_type and work_item_human_id "
        "resolvable in projectctl (preferred), or requirement_ref plus "
        "acceptance_criteria[]. Every RELEASE job MUST depend_on the iteration "
        "INTEGRATION job and is not READY until that INTEGRATION job SUCCEEDED "
        "with a valid candidate. Do not execute engineering work. Do not create "
        "a new project. Do not write files. Reply with the JSON document only.\n"
        f"iteration_human_id hint: {iteration_human_id or 'none'}\n"
    )
    if not work_request:
        return base
    return (
        base
        + "\nWork intake (business intent only). You retain delegated technical "
        "authority: do not ask the operator implementation questions (queues, "
        "job split, architecture, libraries). Put those in assumptions[]. "
        "If sponsor-reserved ambiguity remains (scope expansion, production "
        "release, policy exception, missing testable acceptance), list it in "
        "sponsor_decision_requests[] as {code, question}. Do not invent "
        "sponsor_authority=approved.\n"
        f"business_request: {work_request.get('business_request')}\n"
        f"objective: {work_request.get('objective')}\n"
        f"acceptance: {work_request.get('acceptance')}\n"
        f"sponsor_authority provided: {work_request.get('sponsor_authority') or 'none'}\n"
    )


def _load_latest_accepted_plan(
    conn, project_human_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT plan_json FROM pm_plan_runs
        WHERE project_human_id = ?
          AND status = 'accepted'
          AND dry_run = 0
          AND plan_json IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (project_human_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(str(row["plan_json"]))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def load_latest_accepted_plan(
    conn, project_human_id: str
) -> dict[str, Any] | None:
    """Public replay source for dry-run and inspection."""
    return _load_latest_accepted_plan(conn, project_human_id)


def run_plan(
    *,
    project_human_id: str,
    dry_run: bool = False,
    iteration_human_id: str | None = None,
    db_path: Path | str | None = None,
    registry_path: Path | str | None = None,
    cursor_runner: Callable[..., Any] | None = None,
    projectctl_runner=None,
    plan_override: dict[str, Any] | None = None,
    work_request: dict[str, Any] | None = None,
) -> PlanResult:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    reg_path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    initialize_database(path)

    try:
        validated = resolve_validated_repo(
            project_human_id,
            registry_path=reg_path,
            projectctl_runner=projectctl_runner,
        )
    except ProjectOSError as exc:
        return PlanResult(
            status="error",
            project_human_id=project_human_id,
            dry_run=dry_run,
            error=str(exc),
        )

    known = read_work_item_ids(
        validated.git_root, python_executable=validated.projectctl_python
    )
    output_ref = None
    plan_source = "override"
    plan: dict[str, Any] | None = None

    if plan_override is not None:
        plan = plan_override
        plan_source = "override"
    elif dry_run and work_request is None:
        # Dry-run must not launch engineering work and must remain safe when an
        # accepted plan already exists. Prefer replaying the latest accepted
        # plan (same schema validation, zero job writes). Only invoke Cursor
        # when no accepted plan is available yet. A new work_request skips replay.
        with connection(path) as conn:
            replay = _load_latest_accepted_plan(conn, project_human_id)
        if replay is not None:
            plan = replay
            plan_source = "accepted_replay"
        else:
            prompt = _build_pm_prompt(project_human_id, iteration_human_id)
            cursor = invoke_cursor_agent(
                prompt=prompt,
                workspace=validated.git_root,
                run_id=f"plan-{project_human_id}-{uuid.uuid4().hex[:8]}",
                mode=None,
                output_format="text",
                timeout_seconds=600.0,
                runner=cursor_runner,
                force=True,
            )
            output_ref = cursor.output_ref
            plan_text = plan_text_from_cursor_result(cursor)
            try:
                plan = extract_json_document(plan_text)
                plan_source = "cursor"
            except OrchestrationError as exc:
                return PlanResult(
                    status="error",
                    project_human_id=project_human_id,
                    dry_run=True,
                    error=str(exc),
                    output_ref=output_ref,
                )
    else:
        # Accept path, or dry-run for a new work_request (do not replay).
        prompt = _build_pm_prompt(
            project_human_id,
            iteration_human_id,
            work_request=work_request,
        )
        cursor = invoke_cursor_agent(
            prompt=prompt,
            workspace=validated.git_root,
            run_id=f"plan-{project_human_id}-{uuid.uuid4().hex[:8]}",
            mode=None,
            output_format="text",
            timeout_seconds=600.0,
            runner=cursor_runner,
            force=True,
        )
        output_ref = cursor.output_ref
        if cursor.returncode != 0:
            return PlanResult(
                status="error",
                project_human_id=project_human_id,
                dry_run=dry_run,
                error=f"PM Cursor exit {cursor.returncode}",
                output_ref=output_ref,
            )
        plan_text = plan_text_from_cursor_result(cursor)
        try:
            plan = extract_json_document(plan_text)
            plan_source = "cursor"
        except OrchestrationError as exc:
            return PlanResult(
                status="error",
                project_human_id=project_human_id,
                dry_run=dry_run,
                error=str(exc),
                output_ref=output_ref,
            )

    assert plan is not None

    with connection(path) as conn:

        def dup_check(pid: str, wi: str, queue: str) -> bool:
            # Dry-run must not fail solely because the live plan is already persisted.
            if dry_run:
                return False
            return (
                find_active_assignment(
                    conn, project_human_id=pid, work_item_human_id=wi, queue=queue
                )
                is not None
            )

        errors = validate_plan_document(
            plan,
            expected_project_id=project_human_id,
            known_work_items=known,
            existing_job_checker=dup_check,
        )
        # Persistence uniqueness only applies when we would create jobs.
        if not dry_run:
            for job in plan.get("jobs") or []:
                if isinstance(job, dict) and job.get("human_id"):
                    if get_job_by_human_id(conn, str(job["human_id"])) is not None:
                        errors.append(
                            f"job human_id already exists: {job['human_id']}"
                        )

        conn.execute(
            """
            INSERT INTO pm_plan_runs (
                project_human_id, repository_root, iteration_human_id,
                dry_run, plan_json, output_ref, status, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_human_id,
                str(validated.git_root),
                iteration_human_id or plan.get("iteration_human_id"),
                1 if dry_run else 0,
                json.dumps(plan, sort_keys=True),
                output_ref,
                "rejected" if errors else ("dry_run" if dry_run else "accepted"),
                "; ".join(errors) if errors else None,
            ),
        )

        if errors:
            return PlanResult(
                status="rejected",
                project_human_id=project_human_id,
                dry_run=dry_run,
                plan=plan,
                error="; ".join(errors),
                output_ref=output_ref,
                plan_source=plan_source,
            )

        if dry_run:
            return PlanResult(
                status="dry_run",
                project_human_id=project_human_id,
                dry_run=True,
                plan=plan,
                output_ref=output_ref,
                plan_source=plan_source,
            )

        identity = {
            "project_human_id": validated.entry.project_human_id,
            "repository_root": str(validated.git_root),
            "git_root": str(validated.git_root),
        }
        created: list[str] = []
        id_map: dict[str, int] = {}
        for job in plan["jobs"]:
            queue = str(job["queue"])
            role = str(job.get("agent_role") or QUEUE_TO_ROLE[queue])
            created_job = create_job(
                conn,
                human_id=str(job["human_id"]),
                project_human_id=project_human_id,
                repository_root=validated.git_root,
                agent_role=role,
                queue=queue,
                status="QUEUED" if queue == "RELEASE" else "READY",
                priority=int(job.get("priority", 100)),
                iteration_human_id=iteration_human_id
                or plan.get("iteration_human_id"),
                work_item_type=job.get("work_item_type"),
                work_item_human_id=job.get("work_item_human_id"),
                requires_worktree=role in {
                    "DELIVERY",
                    "ARCHITECTURE",
                    "INTEGRATION",
                    "RELEASE",
                },
                identity_snapshot=identity,
                assignment=_assignment_from_plan_job(job),
                allows_no_change=bool(job.get("allows_no_change", False)),
            )
            try:
                conn.execute(
                    """
                    UPDATE orchestration_jobs
                    SET sponsor_authority = ?
                    WHERE id = ?
                    """,
                    (str(plan.get("sponsor_authority")), created_job.id),
                )
            except Exception:
                pass
            id_map[created_job.human_id] = created_job.id
            created.append(created_job.human_id)

        for job in plan["jobs"]:
            hid = str(job["human_id"])
            for dep in job.get("depends_on") or []:
                add_job_dependency(conn, id_map[hid], id_map[str(dep)])

        return PlanResult(
            status="accepted",
            project_human_id=project_human_id,
            dry_run=False,
            jobs_created=created,
            plan=plan,
            output_ref=output_ref,
            plan_source=plan_source,
        )
