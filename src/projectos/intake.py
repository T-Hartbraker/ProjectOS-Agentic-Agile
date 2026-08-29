"""Work intake: business intent in; PM keeps technical authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from projectos.db import connection
from projectos.migrate import initialize_database
from projectos.plan import PlanResult, run_plan
from projectos.services.context import ServiceContext
from projectos.services.facades import ProjectQueryService

_SPONSOR_GRANT = frozenset(
    {"approved", "granted", "authorized", "sponsor-approved", "sponsor-granted"}
)
_ACCEPTANCE_MARKERS = (
    "must",
    "should",
    "given",
    "when",
    "then",
    "accept",
    "complete when",
    "done when",
    "measurable",
    "cannot ship unless",
    "operator can",
)
_NEW_VENTURE_MARKERS = (
    "new company",
    "new product line",
    "new projectos project",
    "create a new project",
    "stand up a new product",
    "acquire ",
)
_RELEASE_MARKERS = (
    "ship to production",
    "release to customers",
    "launch to production",
    "go live",
)
_POLICY_MARKERS = (
    "hipaa exception",
    "ignore privacy",
    "bypass security",
    "disable audit",
    "store secrets in git",
)


@dataclass(frozen=True)
class DecisionRequest:
    code: str
    question: str
    reserved_for: str = "sponsor"


@dataclass(frozen=True)
class Assumption:
    code: str
    statement: str
    owner: str = "pm"


@dataclass
class IntakeResult:
    status: str
    project_human_id: str
    dry_run: bool
    assumptions: list[dict[str, str]] = field(default_factory=list)
    decision_requests: list[dict[str, str]] = field(default_factory=list)
    expected_jobs: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    plan_source: str | None = None
    jobs_created: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "project_human_id": self.project_human_id,
            "dry_run": self.dry_run,
            "assumptions": self.assumptions,
            "decision_requests": self.decision_requests,
            "expected_jobs": self.expected_jobs,
            "plan": self.plan,
            "plan_source": self.plan_source,
            "jobs_created": self.jobs_created,
            "error": self.error,
        }


def _pm_assumptions() -> list[Assumption]:
    return [
        Assumption(
            "pm_job_graph",
            "The PM owns job graph, queues, roles, and dependencies.",
        ),
        Assumption(
            "pm_implementation",
            "Routine implementation choices stay with PM delegated authority.",
        ),
        Assumption(
            "pm_work_items",
            "The PM maps this request onto projectctl work items when possible.",
        ),
    ]


def _has_testable_acceptance(text: str) -> bool:
    lowered = text.casefold()
    if len(text.strip()) < 24:
        return False
    return any(marker in lowered for marker in _ACCEPTANCE_MARKERS)


def assess_work_request(
    *,
    business_request: str,
    objective: str,
    acceptance: str,
    sponsor_authority: str | None = None,
) -> tuple[list[Assumption], list[DecisionRequest]]:
    """Classify gaps. Does not invent a job graph or health score."""
    assumptions = list(_pm_assumptions())
    decisions: list[DecisionRequest] = []
    request = (business_request or "").strip()
    goal = (objective or "").strip()
    accept = (acceptance or "").strip()
    blob = f"{request}\n{goal}\n{accept}".casefold()
    grant = str(sponsor_authority or "").strip().lower()
    granted = grant in _SPONSOR_GRANT

    if not request:
        decisions.append(
            DecisionRequest(
                "missing_business_request",
                "Sponsor must state the business request this work is meant to satisfy.",
            )
        )
    if not goal:
        decisions.append(
            DecisionRequest(
                "missing_objective",
                "Sponsor must state the objective or outcome this work should achieve.",
            )
        )
    if not accept:
        decisions.append(
            DecisionRequest(
                "missing_acceptance",
                "Sponsor must provide acceptance information that can be checked.",
            )
        )
    elif not _has_testable_acceptance(accept):
        decisions.append(
            DecisionRequest(
                "untestable_acceptance",
                "Sponsor must provide testable acceptance (what must be true when this is done).",
            )
        )
    if any(marker in blob for marker in _NEW_VENTURE_MARKERS):
        decisions.append(
            DecisionRequest(
                "scope_new_venture",
                "This request appears to create a new product or ProjectOS project. Sponsor must confirm that scope.",
            )
        )
    if any(marker in blob for marker in _RELEASE_MARKERS) and not granted:
        decisions.append(
            DecisionRequest(
                "release_authorization",
                "Production/customer release requires an explicit sponsor grant.",
            )
        )
    if any(marker in blob for marker in _POLICY_MARKERS):
        decisions.append(
            DecisionRequest(
                "policy_exception",
                "This request asks for a policy exception reserved to the sponsor.",
            )
        )
    return assumptions, decisions


def _jobs_from_plan(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    if not plan or not isinstance(plan.get("jobs"), list):
        return jobs
    for job in plan["jobs"]:
        if not isinstance(job, dict):
            continue
        hid = job.get("human_id")
        if not isinstance(hid, str) or not hid.strip():
            continue
        jobs.append(
            {
                "human_id": hid,
                "queue": job.get("queue"),
                "agent_role": job.get("agent_role"),
                "depends_on": [str(d) for d in (job.get("depends_on") or [])],
            }
        )
    return jobs


def _merge_decisions(
    assessed: list[DecisionRequest], plan: dict[str, Any] | None
) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for item in assessed:
        merged[item.code] = {
            "code": item.code,
            "question": item.question,
            "reserved_for": item.reserved_for,
        }
    extra = (plan or {}).get("sponsor_decision_requests") or []
    if isinstance(extra, list):
        for raw in extra:
            if isinstance(raw, str) and raw.strip():
                code = "plan_sponsor_decision"
                merged[f"{code}:{raw}"] = {
                    "code": code,
                    "question": raw.strip(),
                    "reserved_for": "sponsor",
                }
            elif isinstance(raw, dict) and raw.get("question"):
                code = str(raw.get("code") or "plan_sponsor_decision")
                merged[code] = {
                    "code": code,
                    "question": str(raw["question"]),
                    "reserved_for": str(raw.get("reserved_for") or "sponsor"),
                }
    return list(merged.values())


def _merge_assumptions(
    assessed: list[Assumption], plan: dict[str, Any] | None
) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for item in assessed:
        merged[item.code] = {
            "code": item.code,
            "statement": item.statement,
            "owner": item.owner,
        }
    extra = (plan or {}).get("assumptions") or []
    if isinstance(extra, list):
        for index, raw in enumerate(extra):
            if isinstance(raw, str) and raw.strip():
                merged[f"plan_{index}"] = {
                    "code": f"plan_{index}",
                    "statement": raw.strip(),
                    "owner": "pm",
                }
            elif isinstance(raw, dict) and raw.get("statement"):
                code = str(raw.get("code") or f"plan_{index}")
                merged[code] = {
                    "code": code,
                    "statement": str(raw["statement"]),
                    "owner": str(raw.get("owner") or "pm"),
                }
    return list(merged.values())


class IntakeService:
    """Project-scoped work intake. Never asks PM-owned implementation questions."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self._projects = ProjectQueryService(ctx)

    def preview(
        self,
        project_human_id: str,
        *,
        business_request: str,
        objective: str,
        acceptance: str,
        iteration_human_id: str | None = None,
        sponsor_authority: str | None = None,
        cursor_runner=None,
        projectctl_runner=None,
    ) -> IntakeResult:
        return self._run(
            project_human_id,
            dry_run=True,
            business_request=business_request,
            objective=objective,
            acceptance=acceptance,
            iteration_human_id=iteration_human_id,
            sponsor_authority=sponsor_authority,
            cursor_runner=cursor_runner,
            projectctl_runner=projectctl_runner,
        )

    def _persist_sponsor_gaps(
        self, project_human_id: str, decision_requests: list[dict[str, str]]
    ) -> None:
        from projectos.decisions import record_intake_decisions

        initialize_database(self.ctx.db_path)
        with connection(self.ctx.db_path) as conn:
            record_intake_decisions(
                conn,
                project_human_id=project_human_id,
                decision_requests=decision_requests,
                requested_by="intake",
            )

    def submit(
        self,
        project_human_id: str,
        *,
        business_request: str,
        objective: str,
        acceptance: str,
        iteration_human_id: str | None = None,
        sponsor_authority: str | None = None,
        cursor_runner=None,
        projectctl_runner=None,
    ) -> IntakeResult:
        return self._run(
            project_human_id,
            dry_run=False,
            business_request=business_request,
            objective=objective,
            acceptance=acceptance,
            iteration_human_id=iteration_human_id,
            sponsor_authority=sponsor_authority,
            cursor_runner=cursor_runner,
            projectctl_runner=projectctl_runner,
        )

    def _run(
        self,
        project_human_id: str,
        *,
        dry_run: bool,
        business_request: str,
        objective: str,
        acceptance: str,
        iteration_human_id: str | None,
        sponsor_authority: str | None,
        cursor_runner,
        projectctl_runner,
    ) -> IntakeResult:
        self._projects._require_project(project_human_id)
        assumptions, decisions = assess_work_request(
            business_request=business_request,
            objective=objective,
            acceptance=acceptance,
            sponsor_authority=sponsor_authority,
        )
        work_request = {
            "business_request": business_request.strip(),
            "objective": objective.strip(),
            "acceptance": acceptance.strip(),
            "sponsor_authority": sponsor_authority,
        }
        if decisions:
            merged = _merge_decisions(decisions, None)
            if not dry_run:
                self._persist_sponsor_gaps(project_human_id, merged)
            return IntakeResult(
                status="needs_sponsor_decision" if not dry_run else "preview",
                project_human_id=project_human_id,
                dry_run=dry_run,
                assumptions=_merge_assumptions(assumptions, None),
                decision_requests=merged,
                error=(
                    None
                    if dry_run
                    else "Sponsor-reserved decisions must be resolved before submit"
                ),
            )

        plan_result = run_plan(
            project_human_id=project_human_id,
            dry_run=dry_run,
            iteration_human_id=iteration_human_id,
            db_path=self.ctx.db_path,
            registry_path=self.ctx.registry_path,
            cursor_runner=cursor_runner,
            projectctl_runner=projectctl_runner,
            work_request=work_request,
        )
        merged_decisions = _merge_decisions(decisions, plan_result.plan)
        merged_assumptions = _merge_assumptions(assumptions, plan_result.plan)
        if not dry_run and merged_decisions:
            self._persist_sponsor_gaps(project_human_id, merged_decisions)
            return IntakeResult(
                status="needs_sponsor_decision",
                project_human_id=project_human_id,
                dry_run=False,
                assumptions=merged_assumptions,
                decision_requests=merged_decisions,
                expected_jobs=_jobs_from_plan(plan_result.plan),
                plan=plan_result.plan,
                plan_source=plan_result.plan_source,
                error="Sponsor-reserved decisions must be resolved before submit",
            )
        if plan_result.status == "error":
            status = "error"
        elif plan_result.status == "rejected":
            status = "rejected"
        elif dry_run:
            status = "needs_sponsor_decision" if merged_decisions else "preview"
        else:
            status = "submitted"
        return IntakeResult(
            status=status,
            project_human_id=project_human_id,
            dry_run=dry_run,
            assumptions=merged_assumptions,
            decision_requests=merged_decisions,
            expected_jobs=_jobs_from_plan(plan_result.plan),
            plan=plan_result.plan,
            plan_source=plan_result.plan_source,
            jobs_created=list(plan_result.jobs_created),
            error=plan_result.error,
        )
