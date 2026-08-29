"""Project-scoped planning and inspection. Path identity is project_human_id only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response

from projectos.errors import OrchestrationError, RegistryError
from projectos.presentation import (
    activity_sentence,
    lane_label,
    next_step_sentence,
    queue_label,
    role_label,
    status_label,
)
from projectos.http.deps import (
    get_approval_service,
    get_control_service,
    get_slack_service,
    get_cursor_runner,
    get_intake_service,
    get_memory_admin_service,
    get_plan_service,
    get_project_query_service,
    get_projectctl_runner,
    get_service_context,
)
from projectos.constants import ASSURANCE_QUEUES
from projectos.http.schemas import (
    ActivityResponse,
    AgentRunResponse,
    ControlActionRequest,
    CurrentStateResponse,
    DispatchEligibleResponse,
    DispatchRunResponse,
    GraphEdge,
    JobGraphResponse,
    JobListResponse,
    JobResponse,
    OrchestrationStatusResponse,
    PlanActionRequest,
    PlanResponse,
    ProjectSummaryResponse,
    QualityResponse,
    ReleaseDetailResponse,
    ReleaseListResponse,
    ReportCatalogResponse,
    ReportDashboardResponse,
    ReportResponse,
    ReportSnapshotListResponse,
    ReportSnapshotSummary,
    LearningResponse,
    MemoryAdminResponse,
    MemoryRetireRequest,
    MemorySupersedeRequest,
    DecisionListResponse,
    DecisionOpenRequest,
    DecisionResolveRequest,
    DecisionResponse,
    SlackBindRequest,
    SlackBindingListResponse,
    SlackBindingResponse,
    SlackNotificationListResponse,
    SlackNotifyResponse,
    SlackUnbindRequest,
    SlackUnbindResponse,
    AuditResponse,
    WorkRequestRequest,
    IntakeResponse,
    RecoveryIdentityCheck,
    RecoveryResponse,
    RecoveryWorktreeAction,
    RunEventListResponse,
    RunEventResponse,
    WorkerRunResponse,
)
from projectos.intake import IntakeService
from projectos.plan import PlanResult
from projectos.services import (
    ApprovalService,
    ControlService,
    MemoryAdminService,
    PlanService,
    ProjectQueryService,
    SlackBindingService,
)
from projectos.store import OrchestrationJob, public_artifact_ref


router = APIRouter(prefix="/v1/projects", tags=["project-ops"])


_DELIVERY_QUEUES = frozenset({"DELIVERY", "ARCHITECTURE", "INTEGRATION"})


def job_lane(queue: str) -> str:
    if queue in ASSURANCE_QUEUES:
        return "assurance"
    if queue in _DELIVERY_QUEUES:
        return "delivery"
    return "control"


def _job_response(job: OrchestrationJob, *, depends_on: list[str] | None = None) -> JobResponse:
    lane = job_lane(job.queue)
    return JobResponse(
        human_id=job.human_id,
        project_human_id=job.project_human_id,
        queue=job.queue,
        agent_role=job.agent_role,
        status=job.status,
        lane=lane,
        iteration_human_id=job.iteration_human_id,
        work_item_type=job.work_item_type,
        work_item_human_id=job.work_item_human_id,
        outcome=job.outcome,
        last_error=job.last_error,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        created_at=job.created_at,
        ready_at=job.ready_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        updated_at=job.updated_at,
        candidate_git_sha=job.candidate_git_sha,
        evidence_ref=public_artifact_ref(job.output_ref),
        depends_on=list(depends_on or []),
        presentation={
            "queue_label": queue_label(job.queue),
            "role_label": role_label(job.agent_role),
            "status_label": status_label(job.status),
            "lane_label": lane_label(lane),
            "activity": activity_sentence(
                queue=job.queue,
                work_item_human_id=job.work_item_human_id,
                status=job.status,
            ),
            "next_step": next_step_sentence(job.queue),
        },
    )


def _depends_map(svc, project_human_id: str) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for job_id, dep in svc.graph(project_human_id).edges:
        mapping.setdefault(job_id, []).append(dep)
    return mapping


def _plan_or_raise(result: PlanResult) -> PlanResult:
    if result.status == "error":
        msg = result.error or "plan failed"
        if "not in the registry" in msg.lower():
            raise RegistryError(msg)
        raise OrchestrationError(msg)
    return result


def _plan_response(result: PlanResult) -> PlanResponse:
    return PlanResponse(
        status=result.status,
        project_human_id=result.project_human_id,
        dry_run=result.dry_run,
        jobs_created=list(result.jobs_created),
        plan=result.plan,
        error=result.error,
        plan_source=result.plan_source,
    )


@router.get("/{project_human_id}/summary", response_model=ProjectSummaryResponse)
def project_summary(
    project_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> ProjectSummaryResponse:
    summary = svc.summary(project_human_id)
    return ProjectSummaryResponse(
        project_human_id=summary.project_human_id,
        enabled=summary.enabled,
        job_counts=summary.job_counts,
        current_iteration_human_id=summary.current_iteration_human_id,
        current_release_job_human_id=summary.current_release_job_human_id,
        current_release_status=summary.current_release_status,
        has_accepted_plan=summary.has_accepted_plan,
    )


@router.get("/{project_human_id}/current", response_model=CurrentStateResponse)
def project_current(
    project_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> CurrentStateResponse:
    current = svc.current(project_human_id)
    return CurrentStateResponse(
        project_human_id=current.project_human_id,
        iteration_human_id=current.iteration_human_id,
        release_job_human_id=current.release_job_human_id,
        release_status=current.release_status,
        from_accepted_plan=current.from_accepted_plan,
    )


@router.post("/{project_human_id}/plan/dry-run", response_model=PlanResponse)
def plan_dry_run(
    project_human_id: str,
    body: PlanActionRequest | None = None,
    plans: PlanService = Depends(get_plan_service),
    projectctl_runner=Depends(get_projectctl_runner),
    cursor_runner=Depends(get_cursor_runner),
) -> PlanResponse:
    payload = body or PlanActionRequest()
    result = _plan_or_raise(
        plans.run(
            project_human_id,
            dry_run=True,
            iteration_human_id=payload.iteration_human_id,
            plan_override=payload.plan,
            projectctl_runner=projectctl_runner,
            cursor_runner=cursor_runner,
        )
    )
    return _plan_response(result)


@router.post("/{project_human_id}/plan/accept", response_model=PlanResponse)
def plan_accept(
    project_human_id: str,
    body: PlanActionRequest | None = None,
    plans: PlanService = Depends(get_plan_service),
    projectctl_runner=Depends(get_projectctl_runner),
    cursor_runner=Depends(get_cursor_runner),
) -> PlanResponse:
    payload = body or PlanActionRequest()
    result = _plan_or_raise(
        plans.run(
            project_human_id,
            dry_run=False,
            iteration_human_id=payload.iteration_human_id,
            plan_override=payload.plan,
            projectctl_runner=projectctl_runner,
            cursor_runner=cursor_runner,
        )
    )
    return _plan_response(result)


def _intake_response(result) -> IntakeResponse:
    return IntakeResponse.model_validate(result.as_dict())


@router.post(
    "/{project_human_id}/work-requests/preview",
    response_model=IntakeResponse,
)
def work_request_preview(
    project_human_id: str,
    body: WorkRequestRequest,
    intake: IntakeService = Depends(get_intake_service),
    projectctl_runner=Depends(get_projectctl_runner),
    cursor_runner=Depends(get_cursor_runner),
) -> IntakeResponse:
    result = intake.preview(
        project_human_id,
        business_request=body.business_request,
        objective=body.objective,
        acceptance=body.acceptance,
        iteration_human_id=body.iteration_human_id,
        sponsor_authority=body.sponsor_authority,
        cursor_runner=cursor_runner,
        projectctl_runner=projectctl_runner,
    )
    return _intake_response(result)


@router.post(
    "/{project_human_id}/work-requests/submit",
    response_model=IntakeResponse,
)
def work_request_submit(
    project_human_id: str,
    body: WorkRequestRequest,
    intake: IntakeService = Depends(get_intake_service),
    projectctl_runner=Depends(get_projectctl_runner),
    cursor_runner=Depends(get_cursor_runner),
) -> IntakeResponse:
    result = intake.submit(
        project_human_id,
        business_request=body.business_request,
        objective=body.objective,
        acceptance=body.acceptance,
        iteration_human_id=body.iteration_human_id,
        sponsor_authority=body.sponsor_authority,
        cursor_runner=cursor_runner,
        projectctl_runner=projectctl_runner,
    )
    return _intake_response(result)


@router.get("/{project_human_id}/jobs", response_model=JobListResponse)
def list_jobs(
    project_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> JobListResponse:
    deps = _depends_map(svc, project_human_id)
    return JobListResponse(
        jobs=[
            _job_response(job, depends_on=deps.get(job.human_id, []))
            for job in svc.jobs(project_human_id)
        ]
    )


@router.get("/{project_human_id}/jobs/{job_human_id}", response_model=JobResponse)
def get_job(
    project_human_id: str,
    job_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> JobResponse:
    deps = _depends_map(svc, project_human_id)
    job = svc.job(project_human_id, job_human_id)
    return _job_response(job, depends_on=deps.get(job.human_id, []))


@router.get("/{project_human_id}/graph", response_model=JobGraphResponse)
def job_graph(
    project_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> JobGraphResponse:
    graph = svc.graph(project_human_id)
    mapping: dict[str, list[str]] = {}
    for job_id, dep in graph.edges:
        mapping.setdefault(job_id, []).append(dep)
    return JobGraphResponse(
        nodes=[
            _job_response(node, depends_on=mapping.get(node.human_id, []))
            for node in graph.nodes
        ],
        edges=[
            GraphEdge(job_human_id=job_id, depends_on=dep)
            for job_id, dep in graph.edges
        ],
    )


@router.get(
    "/{project_human_id}/dispatch/eligible",
    response_model=DispatchEligibleResponse,
)
def dispatch_eligible(
    project_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> DispatchEligibleResponse:
    return DispatchEligibleResponse(
        jobs=[_job_response(j) for j in svc.dispatch_eligible(project_human_id)]
    )


@router.get("/{project_human_id}/events", response_model=RunEventListResponse)
def recent_events(
    project_human_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> RunEventListResponse:
    events = svc.recent_events(project_human_id, limit=limit)
    return RunEventListResponse(
        events=[
            RunEventResponse(
                id=e.id,
                job_human_id=e.job_human_id,
                event_type=e.event_type,
                status=e.status,
                message=e.message,
                created_at=e.created_at,
            )
            for e in events
        ]
    )


def _agent_run_response(row: dict) -> AgentRunResponse:
    return AgentRunResponse(
        job_human_id=row["job_human_id"],
        queue=str(row.get("queue") or ""),
        role=str(row["role"]),
        lane=job_lane(str(row.get("queue") or "")),
        job_status=str(row.get("job_status") or ""),
        exit_code=row.get("exit_code"),
        duration_ms=row.get("duration_ms"),
        error=row.get("error"),
        started_at=row.get("started_at"),
        ended_at=row.get("ended_at"),
        created_at=row.get("created_at"),
        candidate_git_sha=row.get("candidate_git_sha"),
        has_candidate=bool(row.get("has_candidate")),
        evidence_ref=row.get("evidence_ref"),
        prompt_ref=row.get("prompt_ref"),
    )


@router.get("/{project_human_id}/learning", response_model=LearningResponse)
def project_learning(
    project_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> LearningResponse:
    return LearningResponse.model_validate(svc.learning(project_human_id))


@router.post(
    "/{project_human_id}/learning/memories/{memory_human_id}/retire",
    response_model=MemoryAdminResponse,
)
def retire_memory(
    project_human_id: str,
    memory_human_id: str,
    body: MemoryRetireRequest,
    svc: MemoryAdminService = Depends(get_memory_admin_service),
) -> MemoryAdminResponse:
    return MemoryAdminResponse.model_validate(
        svc.retire(
            project_human_id,
            memory_human_id,
            confirmed=body.confirmed,
            reason=body.reason,
            actor=body.actor,
        )
    )


@router.post(
    "/{project_human_id}/learning/memories/{memory_human_id}/supersede",
    response_model=MemoryAdminResponse,
)
def supersede_memory(
    project_human_id: str,
    memory_human_id: str,
    body: MemorySupersedeRequest,
    svc: MemoryAdminService = Depends(get_memory_admin_service),
) -> MemoryAdminResponse:
    return MemoryAdminResponse.model_validate(
        svc.supersede(
            project_human_id,
            memory_human_id,
            successor_title=body.successor_title,
            confirmed=body.confirmed,
            reason=body.reason,
            actor=body.actor,
            evidence_ref=body.evidence_ref,
        )
    )


@router.get("/{project_human_id}/decisions", response_model=DecisionListResponse)
def list_decisions(
    project_human_id: str,
    status: str | None = Query(default=None),
    svc: ApprovalService = Depends(get_approval_service),
) -> DecisionListResponse:
    return DecisionListResponse.model_validate(
        svc.list_decisions(project_human_id, status=status)
    )


@router.post("/{project_human_id}/decisions", response_model=DecisionResponse)
def open_decision(
    project_human_id: str,
    body: DecisionOpenRequest,
    svc: ApprovalService = Depends(get_approval_service),
) -> DecisionResponse:
    return DecisionResponse.model_validate(
        svc.open_decision(
            project_human_id,
            action=body.action,
            reason=body.reason,
            impact=body.impact,
            requested_by=body.requested_by,
            target_kind=body.target_kind,
            target_human_id=body.target_human_id,
        )
    )


@router.get(
    "/{project_human_id}/decisions/{decision_human_id}",
    response_model=DecisionResponse,
)
def get_decision(
    project_human_id: str,
    decision_human_id: str,
    svc: ApprovalService = Depends(get_approval_service),
) -> DecisionResponse:
    return DecisionResponse.model_validate(
        svc.get_decision(project_human_id, decision_human_id)
    )


@router.post(
    "/{project_human_id}/decisions/{decision_human_id}/approve",
    response_model=DecisionResponse,
)
def approve_decision(
    project_human_id: str,
    decision_human_id: str,
    body: DecisionResolveRequest,
    svc: ApprovalService = Depends(get_approval_service),
) -> DecisionResponse:
    return DecisionResponse.model_validate(
        svc.approve_decision(
            project_human_id,
            decision_human_id,
            confirmed=body.confirmed,
            actor=body.actor,
            reason=body.reason,
        )
    )


@router.post(
    "/{project_human_id}/decisions/{decision_human_id}/reject",
    response_model=DecisionResponse,
)
def reject_decision(
    project_human_id: str,
    decision_human_id: str,
    body: DecisionResolveRequest,
    svc: ApprovalService = Depends(get_approval_service),
) -> DecisionResponse:
    return DecisionResponse.model_validate(
        svc.reject_decision(
            project_human_id,
            decision_human_id,
            confirmed=body.confirmed,
            actor=body.actor,
            reason=body.reason,
        )
    )


@router.get(
    "/{project_human_id}/integrations/slack",
    response_model=SlackBindingListResponse,
)
def list_slack_bindings(
    project_human_id: str,
    svc: SlackBindingService = Depends(get_slack_service),
) -> SlackBindingListResponse:
    return SlackBindingListResponse.model_validate(svc.list_bindings(project_human_id))


@router.post(
    "/{project_human_id}/integrations/slack/bind",
    response_model=SlackBindingResponse,
)
def bind_slack(
    project_human_id: str,
    body: SlackBindRequest,
    svc: SlackBindingService = Depends(get_slack_service),
) -> SlackBindingResponse:
    return SlackBindingResponse.model_validate(
        svc.bind(
            project_human_id,
            channel_id=body.channel_id,
            team_id=body.team_id,
            thread_ts=body.thread_ts,
        )
    )


@router.post(
    "/{project_human_id}/integrations/slack/unbind",
    response_model=SlackUnbindResponse,
)
def unbind_slack(
    project_human_id: str,
    body: SlackUnbindRequest,
    svc: SlackBindingService = Depends(get_slack_service),
) -> SlackUnbindResponse:
    return SlackUnbindResponse.model_validate(
        svc.unbind(
            project_human_id,
            channel_id=body.channel_id,
            team_id=body.team_id,
            thread_ts=body.thread_ts,
        )
    )


@router.post(
    "/{project_human_id}/integrations/slack/notify",
    response_model=SlackNotifyResponse,
)
def notify_slack(
    project_human_id: str,
    svc: SlackBindingService = Depends(get_slack_service),
) -> SlackNotifyResponse:
    return SlackNotifyResponse.model_validate(svc.notify(project_human_id))


@router.get(
    "/{project_human_id}/integrations/slack/notifications",
    response_model=SlackNotificationListResponse,
)
def list_slack_notifications(
    project_human_id: str,
    svc: SlackBindingService = Depends(get_slack_service),
) -> SlackNotificationListResponse:
    return SlackNotificationListResponse.model_validate(
        svc.list_notifications(project_human_id)
    )


@router.get("/{project_human_id}/audit", response_model=AuditResponse)
def project_audit(
    project_human_id: str,
    request: Request,
    actor_type: str | None = Query(default=None),
    action: str | None = Query(default=None),
    entity_kind: str | None = Query(default=None),
    entity_human_id: str | None = Query(default=None),
    iteration_human_id: str | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=300),
) -> AuditResponse:
    from projectos.audit import list_audit

    ctx = get_service_context(request)
    return AuditResponse.model_validate(
        list_audit(
            ctx,
            project_human_id,
            actor_type=actor_type,
            action=action,
            entity_kind=entity_kind,
            entity_human_id=entity_human_id,
            iteration_human_id=iteration_human_id,
            source=source,
            limit=limit,
        )
    )


@router.get("/{project_human_id}/quality", response_model=QualityResponse)
def project_quality(
    project_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> QualityResponse:
    snapshot = svc.quality(project_human_id)
    return QualityResponse.model_validate(snapshot)


@router.get("/{project_human_id}/reports", response_model=ReportCatalogResponse)
def list_reports(
    project_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> ReportCatalogResponse:
    return ReportCatalogResponse.model_validate(svc.report_catalog(project_human_id))


@router.get("/{project_human_id}/reports/dashboard", response_model=ReportDashboardResponse)
def report_dashboard(
    project_human_id: str,
    iteration_human_id: str | None = Query(default=None),
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> ReportDashboardResponse:
    return ReportDashboardResponse.model_validate(
        svc.report_dashboard(project_human_id, iteration_human_id=iteration_human_id)
    )


@router.get("/{project_human_id}/reports/snapshots", response_model=ReportSnapshotListResponse)
def list_report_snapshots(
    project_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> ReportSnapshotListResponse:
    return ReportSnapshotListResponse.model_validate(svc.report_snapshots(project_human_id))


@router.get(
    "/{project_human_id}/reports/snapshots/{snapshot_human_id}/download",
)
def download_report_snapshot(
    project_human_id: str,
    snapshot_human_id: str,
    format: str = Query(default="html"),
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> Response:
    payload = svc.report_snapshot_download(
        project_human_id, snapshot_human_id, fmt=format
    )
    filename = payload["filename"]
    return Response(
        content=payload["content"],
        media_type=str(payload["media_type"]),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "X-Report-Revision": str(payload.get("revision") or ""),
        },
    )


@router.get(
    "/{project_human_id}/reports/snapshots/{snapshot_human_id}",
    response_model=ReportResponse,
)
def get_report_snapshot(
    project_human_id: str,
    snapshot_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> ReportResponse:
    return ReportResponse.model_validate(
        svc.report_snapshot(project_human_id, snapshot_human_id)
    )


@router.post(
    "/{project_human_id}/reports/{kind}/snapshots",
    response_model=ReportSnapshotSummary,
)
def save_report_snapshot(
    project_human_id: str,
    kind: str,
    iteration_human_id: str | None = Query(default=None),
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> ReportSnapshotSummary:
    return ReportSnapshotSummary.model_validate(
        svc.save_report_snapshot(
            project_human_id, kind, iteration_human_id=iteration_human_id
        )
    )


@router.get("/{project_human_id}/reports/{kind}", response_model=ReportResponse)
def get_report(
    project_human_id: str,
    kind: str,
    iteration_human_id: str | None = Query(default=None),
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> ReportResponse:
    return ReportResponse.model_validate(
        svc.report(
            project_human_id,
            kind,
            iteration_human_id=iteration_human_id,
        )
    )


@router.get("/{project_human_id}/reports/{kind}/download")
def download_report(
    project_human_id: str,
    kind: str,
    format: str = Query(default="html"),
    iteration_human_id: str | None = Query(default=None),
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> Response:
    payload = svc.report_download(
        project_human_id,
        kind,
        fmt=format,
        iteration_human_id=iteration_human_id,
    )
    filename = payload["filename"]
    return Response(
        content=payload["content"],
        media_type=str(payload["media_type"]),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "X-Report-Revision": str(payload.get("revision") or ""),
        },
    )


@router.get("/{project_human_id}/releases", response_model=ReleaseListResponse)
def list_releases(
    project_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> ReleaseListResponse:
    return ReleaseListResponse.model_validate(svc.releases(project_human_id))


@router.get(
    "/{project_human_id}/releases/{release_human_id}",
    response_model=ReleaseDetailResponse,
)
def get_release(
    project_human_id: str,
    release_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> ReleaseDetailResponse:
    return ReleaseDetailResponse.model_validate(
        svc.release(project_human_id, release_human_id)
    )


@router.get("/{project_human_id}/releases/{release_human_id}/artifacts/{artifact_human_id}")
def download_release_artifact(
    project_human_id: str,
    release_human_id: str,
    artifact_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> Response:
    artifact = svc.release_artifact(
        project_human_id, release_human_id, artifact_human_id
    )
    filename = artifact["filename"]
    return Response(
        content=artifact["content"],
        media_type=str(artifact["media_type"]),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "X-Artifact-Sha256": artifact["sha256"],
        },
    )


@router.get("/{project_human_id}/activity", response_model=ActivityResponse)
def project_activity(
    project_human_id: str,
    svc: ProjectQueryService = Depends(get_project_query_service),
) -> ActivityResponse:
    deps = _depends_map(svc, project_human_id)
    jobs = svc.jobs(project_human_id)
    in_flight = [
        _job_response(job, depends_on=deps.get(job.human_id, []))
        for job in jobs
        if job.status in {"LEASED", "RUNNING"}
    ]
    runs = svc.agent_runs(project_human_id, limit=40)
    events = svc.recent_events(project_human_id, limit=40)
    return ActivityResponse(
        in_flight=in_flight,
        recent_runs=[_agent_run_response(row) for row in runs],
        recent_events=[
            RunEventResponse(
                id=e.id,
                job_human_id=e.job_human_id,
                event_type=e.event_type,
                status=e.status,
                message=e.message,
                created_at=e.created_at,
            )
            for e in events
        ],
    )


def _idempotency_key(
    body: ControlActionRequest | None, header_key: str | None
) -> str | None:
    if header_key and header_key.strip():
        return header_key.strip()
    if body and body.idempotency_key:
        return body.idempotency_key
    return None


def _dispatch_response(payload: dict, *, replayed: bool = False) -> DispatchRunResponse:
    return DispatchRunResponse(
        mode=payload["mode"],
        message=payload["message"],
        cancelled=bool(payload["cancelled"]),
        completed=[WorkerRunResponse(**item) for item in payload.get("completed") or []],
        replayed=replayed,
    )


def _recovery_response(payload: dict, *, replayed: bool = False) -> RecoveryResponse:
    return RecoveryResponse(
        project_human_id=payload["project_human_id"],
        dry_run=bool(payload["dry_run"]),
        ok=bool(payload["ok"]),
        expired_lease_job_ids=list(payload.get("expired_lease_job_ids") or []),
        promoted_ready=list(payload.get("promoted_ready") or []),
        blocked=list(payload.get("blocked") or []),
        identity_checks=[
            RecoveryIdentityCheck(**item) for item in payload.get("identity_checks") or []
        ],
        worktree_actions=[
            RecoveryWorktreeAction(**item) for item in payload.get("worktree_actions") or []
        ],
        messages=list(payload.get("messages") or []),
        replayed=replayed,
    )


@router.post(
    "/{project_human_id}/dispatch/run-once",
    response_model=DispatchRunResponse,
)
def dispatch_run_once(
    project_human_id: str,
    body: ControlActionRequest | None = None,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    svc: ControlService = Depends(get_control_service),
) -> DispatchRunResponse:
    payload = body or ControlActionRequest()
    key = _idempotency_key(payload, idempotency_key_header)
    fingerprint = {"op": "dispatch_run_once", "job_human_id": payload.job_human_id}
    replay = svc.replay_or_begin(
        project_human_id=project_human_id,
        operation="dispatch_run_once",
        idempotency_key=key,
        fingerprint=fingerprint,
    )
    if replay is not None:
        return _dispatch_response(replay.payload, replayed=True)
    result = svc.dispatch_run_once(
        project_human_id, job_human_id=payload.job_human_id
    )
    svc.remember(
        project_human_id=project_human_id,
        operation="dispatch_run_once",
        idempotency_key=key,
        fingerprint=fingerprint,
        status_code=200,
        payload=result,
    )
    return _dispatch_response(result)


@router.post(
    "/{project_human_id}/dispatch/run-until-idle",
    response_model=DispatchRunResponse,
)
def dispatch_run_until_idle(
    project_human_id: str,
    body: ControlActionRequest | None = None,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    svc: ControlService = Depends(get_control_service),
) -> DispatchRunResponse:
    payload = body or ControlActionRequest()
    key = _idempotency_key(payload, idempotency_key_header)
    parallel = payload.max_parallel or 3
    fingerprint = {"op": "dispatch_run_until_idle", "max_parallel": parallel}
    replay = svc.replay_or_begin(
        project_human_id=project_human_id,
        operation="dispatch_run_until_idle",
        idempotency_key=key,
        fingerprint=fingerprint,
    )
    if replay is not None:
        return _dispatch_response(replay.payload, replayed=True)
    result = svc.dispatch_run_until_idle(
        project_human_id, max_parallel=parallel
    )
    svc.remember(
        project_human_id=project_human_id,
        operation="dispatch_run_until_idle",
        idempotency_key=key,
        fingerprint=fingerprint,
        status_code=200,
        payload=result,
    )
    return _dispatch_response(result)


@router.get(
    "/{project_human_id}/orchestration",
    response_model=OrchestrationStatusResponse,
)
def orchestration_status(
    project_human_id: str,
    svc: ControlService = Depends(get_control_service),
) -> OrchestrationStatusResponse:
    payload = svc.orchestration_status(project_human_id)
    return OrchestrationStatusResponse(**payload)


@router.post(
    "/{project_human_id}/orchestration/pause",
    response_model=OrchestrationStatusResponse,
)
def orchestration_pause(
    project_human_id: str,
    body: ControlActionRequest | None = None,
    svc: ControlService = Depends(get_control_service),
) -> OrchestrationStatusResponse:
    payload = body or ControlActionRequest()
    return OrchestrationStatusResponse(
        **svc.pause(project_human_id, reason=payload.reason)
    )


@router.post(
    "/{project_human_id}/orchestration/resume",
    response_model=OrchestrationStatusResponse,
)
def orchestration_resume(
    project_human_id: str,
    svc: ControlService = Depends(get_control_service),
) -> OrchestrationStatusResponse:
    return OrchestrationStatusResponse(**svc.resume(project_human_id))


@router.get(
    "/{project_human_id}/recovery/preview",
    response_model=RecoveryResponse,
)
def recovery_preview(
    project_human_id: str,
    svc: ControlService = Depends(get_control_service),
) -> RecoveryResponse:
    return _recovery_response(svc.recovery_preview(project_human_id))


@router.post(
    "/{project_human_id}/recovery/execute",
    response_model=RecoveryResponse,
)
def recovery_execute(
    project_human_id: str,
    body: ControlActionRequest | None = None,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    svc: ControlService = Depends(get_control_service),
) -> RecoveryResponse:
    payload = body or ControlActionRequest()
    key = _idempotency_key(payload, idempotency_key_header)
    fingerprint = {"op": "recovery_execute"}
    replay = svc.replay_or_begin(
        project_human_id=project_human_id,
        operation="recovery_execute",
        idempotency_key=key,
        fingerprint=fingerprint,
    )
    if replay is not None:
        return _recovery_response(replay.payload, replayed=True)
    result = svc.recovery_execute(project_human_id)
    svc.remember(
        project_human_id=project_human_id,
        operation="recovery_execute",
        idempotency_key=key,
        fingerprint=fingerprint,
        status_code=200,
        payload=result,
    )
    return _recovery_response(result)

