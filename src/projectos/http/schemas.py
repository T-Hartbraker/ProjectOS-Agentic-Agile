"""HTTP request/response models. No filesystem payloads beyond an absolute repo path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthComponent(BaseModel):
    name: str
    status: str
    required: bool = False
    detail: str = ""
    pid: int | None = None


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    ready: bool = False
    notice: str | None = None
    components: list[HealthComponent] = Field(default_factory=list)


class ProjectResponse(BaseModel):
    project_human_id: str
    repository_root: str
    enabled: bool


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]


class OnboardingResponse(ProjectResponse):
    action: str
    git_root: str
    project_name: str | None = None


class RegisterProjectRequest(BaseModel):
    """Governed onboarding. Absolute delivery-repo path only — not a file-read API."""

    model_config = ConfigDict(extra="forbid")

    repository_path: str = Field(min_length=1)

    @field_validator("repository_path")
    @classmethod
    def absolute_repository_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("repository_path must be an absolute filesystem path")
        # Reject traversal-style relative segments used as a "path" even when
        # the string looks absolute after expansion; Path.is_absolute is enough
        # for the API gate. Onboarding still discovers the git root and
        # refuses identity mismatches.
        return value


class PlanActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration_human_id: str | None = None
    plan: dict | None = None
    work_request: dict | None = None

    @field_validator("plan")
    @classmethod
    def reject_repository_root(cls, value: dict | None) -> dict | None:
        if value is not None and "repository_root" in value:
            raise ValueError("repository_root is not accepted")
        return value


class PlanResponse(BaseModel):
    status: str
    project_human_id: str
    dry_run: bool
    jobs_created: list[str]
    plan: dict | None = None
    error: str | None = None
    plan_source: str | None = None


class WorkRequestRequest(BaseModel):
    """Business intent only. Implementation fields are PM-owned and rejected."""

    model_config = ConfigDict(extra="forbid")

    business_request: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    acceptance: str = Field(min_length=1)
    iteration_human_id: str | None = None
    sponsor_authority: str | None = None

    @field_validator("business_request", "objective", "acceptance")
    @classmethod
    def reject_path_like(cls, value: str) -> str:
        if "repository_root" in value.casefold():
            raise ValueError("repository_root is not accepted in work requests")
        return value


class IntakeAssumption(BaseModel):
    code: str
    statement: str
    owner: str


class IntakeDecisionRequest(BaseModel):
    code: str
    question: str
    reserved_for: str


class IntakeExpectedJob(BaseModel):
    human_id: str
    queue: str | None = None
    agent_role: str | None = None
    depends_on: list[str] = Field(default_factory=list)


class IntakeResponse(BaseModel):
    status: str
    project_human_id: str
    dry_run: bool
    assumptions: list[IntakeAssumption]
    decision_requests: list[IntakeDecisionRequest]
    expected_jobs: list[IntakeExpectedJob]
    plan: dict | None = None
    plan_source: str | None = None
    jobs_created: list[str]
    error: str | None = None


class ProjectSummaryResponse(BaseModel):
    project_human_id: str
    enabled: bool
    job_counts: dict[str, int]
    current_iteration_human_id: str | None = None
    current_release_job_human_id: str | None = None
    current_release_status: str | None = None
    has_accepted_plan: bool


class CurrentStateResponse(BaseModel):
    project_human_id: str
    iteration_human_id: str | None = None
    release_job_human_id: str | None = None
    release_status: str | None = None
    from_accepted_plan: bool


class JobPresentation(BaseModel):
    """Display-only labels. Canonical IDs remain on JobResponse."""

    queue_label: str
    role_label: str
    status_label: str
    lane_label: str
    activity: str
    next_step: str


class JobResponse(BaseModel):
    human_id: str
    project_human_id: str
    queue: str
    agent_role: str
    status: str
    lane: str
    iteration_human_id: str | None = None
    work_item_type: str | None = None
    work_item_human_id: str | None = None
    outcome: str | None = None
    last_error: str | None = None
    attempt: int = 0
    max_attempts: int = 0
    created_at: str | None = None
    ready_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str | None = None
    candidate_git_sha: str | None = None
    evidence_ref: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    presentation: JobPresentation | None = None


class JobListResponse(BaseModel):
    jobs: list[JobResponse]


class GraphEdge(BaseModel):
    job_human_id: str
    depends_on: str


class JobGraphResponse(BaseModel):
    nodes: list[JobResponse]
    edges: list[GraphEdge]


class DispatchEligibleResponse(BaseModel):
    jobs: list[JobResponse]


class RunEventResponse(BaseModel):
    id: int
    job_human_id: str
    event_type: str
    status: str | None = None
    message: str | None = None
    created_at: str


class RunEventListResponse(BaseModel):
    events: list[RunEventResponse]


class AgentRunResponse(BaseModel):
    job_human_id: str
    queue: str
    role: str
    lane: str
    job_status: str
    exit_code: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    created_at: str | None = None
    candidate_git_sha: str | None = None
    has_candidate: bool = False
    evidence_ref: str | None = None
    prompt_ref: str | None = None


class AgentRunListResponse(BaseModel):
    runs: list[AgentRunResponse]


class ActivityResponse(BaseModel):
    in_flight: list[JobResponse]
    recent_runs: list[AgentRunResponse]
    recent_events: list[RunEventResponse]


class QualitySummary(BaseModel):
    required_roles: list[str]
    role_results: dict[str, str]
    pending_count: int
    passed_count: int
    failed_count: int
    stale_count: int
    open_assurance_jobs: int
    evaluated_candidate_shas: list[str] = Field(default_factory=list)


class QualityEvidenceItem(BaseModel):
    assurance_role: str
    result: str
    candidate_git_sha: str | None = None
    evidence_ref: str | None = None
    delivery_job_human_id: str | None = None
    assurance_job_human_id: str | None = None
    assurance_job_status: str | None = None
    defect_human_id: str | None = None
    created_at: str | None = None


class QualityFinding(BaseModel):
    role: str
    result: str
    candidate_git_sha: str | None = None
    evidence_ref: str | None = None
    job_human_id: str | None = None


class QualityDefect(BaseModel):
    defect_human_id: str
    severity: str
    priority: str
    status: str
    assurance_role: str | None = None
    delivery_job_human_id: str | None = None
    candidate_git_sha: str | None = None
    result: str | None = None


class QualityLineageItem(BaseModel):
    kind: str
    delivery_job_human_id: str | None = None
    assurance_job_human_id: str | None = None
    rework_job_human_id: str | None = None
    retest_job_human_id: str | None = None
    candidate_git_sha: str | None = None
    invalidated_candidate_sha: str | None = None
    reason: str | None = None
    status: str | None = None


class QualityResponse(BaseModel):
    """Independent QA/defect read model. No pass/fail write fields."""

    project_human_id: str
    qa_pass_authority: str
    developer_can_mark_qa_passed: bool
    summary: QualitySummary
    evidence: list[QualityEvidenceItem]
    findings: dict[str, QualityFinding | None]
    defects: list[QualityDefect]
    defect_counts: dict[str, dict[str, int]]
    lineage: list[QualityLineageItem]
    release_blocking_reasons: list[str]


class ReleaseArtifactRef(BaseModel):
    artifact_human_id: str
    filename: str
    sha256: str
    byte_size: int
    media_type: str
    kind: str
    download_ref: str


class ReleaseSummary(BaseModel):
    release_human_id: str
    job_human_id: str
    iteration_human_id: str | None = None
    status: str
    outcome: str | None = None
    gate: str
    integrated_sha: str | None = None
    released_sha: str | None = None
    qa_recommendation: str
    artifact_count: int = 0
    updated_at: str | None = None


class ReleaseListResponse(BaseModel):
    project_human_id: str
    releases: list[ReleaseSummary]


class ReleaseManifestFile(BaseModel):
    artifact_human_id: str
    filename: str
    sha256: str
    byte_size: int


class ReleaseManifest(BaseModel):
    release_human_id: str
    integrated_sha: str | None = None
    released_sha: str | None = None
    files: list[ReleaseManifestFile] = Field(default_factory=list)


class ReleaseChecksum(BaseModel):
    filename: str
    sha256: str


class ReleaseQaRecommendation(BaseModel):
    status: str
    reasons: list[str] = Field(default_factory=list)


class ReleaseFinding(BaseModel):
    kind: str
    result: str | None = None
    role: str | None = None
    candidate_git_sha: str | None = None
    evidence_ref: str | None = None


class ReleaseDetailResponse(ReleaseSummary):
    project_human_id: str
    qa_recommendation_detail: ReleaseQaRecommendation
    known_findings: list[ReleaseFinding] = Field(default_factory=list)
    release_notes: str | None = None
    migration_notes: str | None = None
    rollback_notes: str | None = None
    manifest: ReleaseManifest
    checksums: list[ReleaseChecksum] = Field(default_factory=list)
    artifacts: list[ReleaseArtifactRef] = Field(default_factory=list)
    last_error: str | None = None


class ReportCatalogItem(BaseModel):
    kind: str
    title: str


class ReportCatalogResponse(BaseModel):
    project_human_id: str
    download_formats: list[str] = Field(default_factory=list)
    reports: list[ReportCatalogItem]


class ReportSource(BaseModel):
    entity_type: str
    entity_human_id: str
    timestamp: str | None = None


class ReportResponse(BaseModel):
    """Collected report envelope. Rendering is a separate step."""

    schema_version: int
    report_kind: str
    title: str
    project_human_id: str
    iteration_human_id: str | None = None
    release_human_id: str | None = None
    generated_at: str
    revision: str
    origin: str = "live"
    snapshot_human_id: str | None = None
    saved_at: str | None = None
    sources: list[ReportSource] = Field(default_factory=list)
    body: dict[str, Any] = Field(default_factory=dict)


class ReportSnapshotSummary(BaseModel):
    snapshot_human_id: str
    project_human_id: str
    report_kind: str
    revision: str
    iteration_human_id: str | None = None
    release_human_id: str | None = None
    generated_at: str
    saved_at: str
    origin: str = "snapshot"


class ReportSnapshotListResponse(BaseModel):
    origin: str = "snapshot"
    project_human_id: str
    snapshots: list[ReportSnapshotSummary]


class ReportDashboardResponse(BaseModel):
    origin: str = "live"
    project_human_id: str
    generated_at: str
    iteration_human_id: str | None = None
    notice: str
    download_formats: list[str] = Field(default_factory=list)
    reports: list[ReportResponse]
    snapshots: list[ReportSnapshotSummary] = Field(default_factory=list)


class LearningMemory(BaseModel):
    memory_human_id: str
    project_human_id: str
    agent_role: str
    memory_kind: str
    title: str
    evidence_ref: str | None = None
    source_job_human_id: str | None = None
    confidence: float
    occurrence_count: int
    last_validated_at: str | None = None
    status: str
    promotion_mode: str
    rejection_code: str | None = None
    rejection_reason: str | None = None
    superseded_by_memory_human_id: str | None = None
    created_at: str
    updated_at: str


class LearningEvent(BaseModel):
    project_human_id: str
    memory_human_id: str
    event_type: str
    job_human_id: str | None = None
    actor: str | None = None
    rejection_code: str | None = None
    rejection_reason: str | None = None
    created_at: str


class LearningInjection(BaseModel):
    project_human_id: str
    memory_human_id: str
    job_human_id: str
    agent_run_id: int | None = None
    created_at: str


class LearningResponse(BaseModel):
    project_human_id: str
    notice: str
    active_memories: list[LearningMemory]
    rejected_memories: list[LearningMemory]
    retired_memories: list[LearningMemory] = Field(default_factory=list)
    superseded_memories: list[LearningMemory] = Field(default_factory=list)
    events: list[LearningEvent]
    injected_in_recent_runs: list[LearningInjection]


class MemoryRetireRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    confirmed: bool
    reason: str = Field(min_length=1)
    actor: str = Field(min_length=1)


class MemorySupersedeRequest(MemoryRetireRequest):
    successor_title: str = Field(min_length=1)
    evidence_ref: str | None = None


class MemoryAdminResponse(BaseModel):
    project_human_id: str
    action: str
    actor: str
    reason: str
    memory: LearningMemory
    successor: LearningMemory | None = None


class DecisionEvent(BaseModel):
    project_human_id: str
    decision_human_id: str
    event_type: str
    actor: str | None = None
    reason: str | None = None
    created_at: str


class DecisionResponse(BaseModel):
    decision_human_id: str
    project_human_id: str
    action: str
    target_kind: str
    target_human_id: str | None = None
    reason: str
    impact: str
    requested_by: str
    status: str
    decided_by: str | None = None
    decision_reason: str | None = None
    execution_result: str | None = None
    created_at: str
    updated_at: str
    decided_at: str | None = None
    notice: str
    events: list[DecisionEvent] = Field(default_factory=list)


class DecisionListResponse(BaseModel):
    project_human_id: str
    notice: str
    decisions: list[DecisionResponse]


class DecisionOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)
    target_kind: str = "none"
    target_human_id: str | None = None

    @field_validator("action", "target_kind", "target_human_id", "requested_by")
    @classmethod
    def reject_path_fields(cls, value: str | None) -> str | None:
        if value is not None and ("/" in value or "\\" in value or ".." in value):
            raise ValueError("must not contain a path")
        return value


class DecisionResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: bool
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("actor")
    @classmethod
    def reject_path_actor(cls, value: str) -> str:
        if "/" in value or "\\" in value or ".." in value:
            raise ValueError("actor must not contain a path")
        return value


class SlackBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    team_id: str | None = None
    thread_ts: str | None = None

    @field_validator("channel_id", "team_id", "thread_ts")
    @classmethod
    def reject_path_slack_id(cls, value: str | None) -> str | None:
        if value is not None and ("/" in value or "\\" in value or ".." in value):
            raise ValueError("must not contain a path")
        return value


class SlackUnbindRequest(SlackBindRequest):
    pass


class SlackWorkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_request: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    acceptance: str = Field(min_length=1)


class SlackInboundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    team_id: str | None = None
    thread_ts: str | None = None
    message_ts: str | None = None
    project_human_id: str | None = None
    work_request: SlackWorkRequest | None = None

    @field_validator("channel_id", "team_id", "thread_ts", "message_ts", "project_human_id")
    @classmethod
    def reject_path_inbound(cls, value: str | None) -> str | None:
        if value is not None and ("/" in value or "\\" in value or ".." in value):
            raise ValueError("must not contain a path")
        return value


class SlackBindingResponse(BaseModel):
    binding_human_id: str
    project_human_id: str
    team_id: str | None = None
    channel_id: str
    thread_ts: str | None = None
    created_at: str
    updated_at: str
    repository_root: str | None = None
    repository_source: str | None = None
    notice: str | None = None


class SlackMessageRef(BaseModel):
    project_human_id: str
    team_id: str | None = None
    channel_id: str
    thread_ts: str | None = None
    message_ts: str
    created_at: str | None = None


class SlackBindingListResponse(BaseModel):
    project_human_id: str
    notice: str
    repository_root: str
    repository_source: str
    bindings: list[SlackBindingResponse]
    message_refs: list[SlackMessageRef] = Field(default_factory=list)


class SlackBoundChannel(BaseModel):
    project_human_id: str
    channel_id: str
    team_id: str | None = None
    thread_ts: str | None = None
    channel_name: str | None = None


class SlackStatusResponse(BaseModel):
    mode: str
    connection_status: str
    app_token: str
    bot_token: str
    workspace_name: str | None = None
    team_id: str | None = None
    bound_channel: SlackBoundChannel | None = None
    bound_channels: list[SlackBoundChannel] = Field(default_factory=list)
    interface_channels: list["SlackInterfaceChannel"] = Field(default_factory=list)
    default_channel_id: str | None = None
    setup_steps: list[str] = Field(default_factory=list)
    detail: str = ""


class SlackInterfaceChannel(BaseModel):
    channel_id: str
    team_id: str | None = None
    is_default: bool = False


class SlackSettingsResponse(BaseModel):
    enabled: bool
    mode: str
    transport: str
    connection_status: str
    detail: str = ""
    workspace_name: str | None = None
    team_id: str | None = None
    connection_updated_at: str | None = None
    app_token: str
    bot_token: str
    app_token_present: bool
    bot_token_present: bool
    app_token_valid_prefix: bool
    bot_token_valid_prefix: bool
    signing_secret_present: bool = False
    app_token_configured: bool = False
    bot_token_configured: bool = False
    app_token_source: str = "none"
    bot_token_source: str = "none"
    configured: bool = False
    tokens_ready: bool = False
    connection_state: str = "not_configured"
    storage: str = "none"
    interface_channels: list[SlackInterfaceChannel] = Field(default_factory=list)
    default_channel_id: str | None = None
    bound_channels: list[SlackBoundChannel] = Field(default_factory=list)
    setup_steps: list[str] = Field(default_factory=list)


class SlackInterfaceChannelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    team_id: str | None = None
    is_default: bool = False


class SlackInterfaceChannelRemove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    team_id: str | None = None


class SlackSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    add_interface_channels: list[SlackInterfaceChannelInput] = Field(default_factory=list)
    remove_interface_channels: list[SlackInterfaceChannelRemove] = Field(default_factory=list)
    default_channel_id: str | None = None
    default_team_id: str | None = None


class SlackTokensUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_token: str | None = None
    bot_token: str | None = None
    signing_secret: str | None = None


class SlackTokensUpdateResponse(BaseModel):
    ok: bool
    updated_fields: list[str] = Field(default_factory=list)
    restart_required: bool = True
    notice: str = ""
    storage: str
    app_token: str
    bot_token: str
    app_token_present: bool
    bot_token_present: bool
    app_token_valid_prefix: bool
    bot_token_valid_prefix: bool
    signing_secret_present: bool = False
    app_token_source: str = "none"
    bot_token_source: str = "none"


class SlackTestResponse(BaseModel):
    ok: bool
    app_token: str
    bot_token: str
    socket_mode: str
    workspace: str | None = None
    team_id: str | None = None
    detail: str = ""


class OpenAISettingsResponse(BaseModel):
    enabled: bool
    api_key_configured: bool
    api_key_source: str
    model: str
    supported_models: list[str] = Field(default_factory=list)
    slack_chatgpt_user_id: str
    slack_chatgpt_user_id_source: str
    last_test_status: str | None = None
    last_test_at: str | None = None
    last_error: str | None = None
    setup_steps: list[str] = Field(default_factory=list)


class OpenAISettingsUpdateRequest(BaseModel):
    model: str | None = None
    slack_chatgpt_user_id: str | None = None


class OpenAISecretPutRequest(BaseModel):
    api_key: str | None = None


class OpenAISecretDeleteResponse(BaseModel):
    ok: bool
    api_key_configured: bool
    api_key_source: str
    notice: str


class OpenAISecretPutResponse(BaseModel):
    ok: bool
    api_key_configured: bool
    api_key_source: str
    notice: str


class OpenAITestResponse(BaseModel):
    ok: bool
    detail: str | None = None
    response_id: str | None = None


class SlackUnbindResponse(BaseModel):
    ok: bool
    project_human_id: str
    binding_human_id: str


class SlackInboundResponse(BaseModel):
    project_human_id: str
    binding_human_id: str | None = None
    resolved_via: str
    notice: str
    repository_root: str
    repository_source: str
    enabled: bool
    message_ref: SlackMessageRef | None = None
    intake: dict[str, Any] | None = None


class SlackCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    team_id: str | None = None
    thread_ts: str | None = None
    message_ts: str | None = None
    project_human_id: str | None = None
    title: str | None = None
    description: str | None = None
    source: str | None = None

    @field_validator(
        "command",
        "channel_id",
        "team_id",
        "thread_ts",
        "message_ts",
        "project_human_id",
        "source",
    )
    @classmethod
    def reject_path_command(cls, value: str | None) -> str | None:
        if value is not None and ("/" in value or "\\" in value or ".." in value):
            raise ValueError("must not contain a path")
        return value


class SlackCommandResponse(BaseModel):
    project_human_id: str
    command: str
    text: str
    notice: str
    resolved_via: str
    binding_human_id: str | None = None
    item_kind: str | None = None
    item_human_id: str | None = None
    idempotent: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    message_ref: SlackMessageRef | None = None


class SlackNotification(BaseModel):
    notification_human_id: str
    project_human_id: str
    kind: str
    entity_human_id: str
    channel_id: str
    team_id: str | None = None
    thread_ts: str | None = None
    text: str
    dashboard_path: str
    created_at: str


class SlackNotifyResponse(BaseModel):
    project_human_id: str
    posted: list[SlackNotification]
    already_posted: list[str] = Field(default_factory=list)
    notice: str


class SlackNotificationListResponse(BaseModel):
    project_human_id: str
    notifications: list[SlackNotification]


class AuditEvent(BaseModel):
    occurred_at: str
    actor_type: str
    actor_id: str
    action: str
    entity_kind: str
    entity_human_id: str
    iteration_human_id: str | None = None
    source: str


class AuditResponse(BaseModel):
    project_human_id: str
    notice: str
    events: list[AuditEvent]


class PortfolioProjectCard(BaseModel):
    project_human_id: str
    enabled: bool
    health: str
    current_iteration_human_id: str | None = None
    blocker_count: int
    release_human_id: str | None = None
    release_status: str | None = None
    active_job_count: int
    open_defect_count: int
    active_memory_count: int


class PortfolioResponse(BaseModel):
    notice: str
    projects: list[PortfolioProjectCard]



class ControlActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str | None = None
    job_human_id: str | None = None
    max_parallel: int | None = Field(default=None, ge=1, le=8)
    reason: str | None = None

    @field_validator("job_human_id")
    @classmethod
    def reject_path_job_id(cls, value: str | None) -> str | None:
        if value is not None and ("/" in value or "\\" in value or ".." in value):
            raise ValueError("job_human_id must not contain a path")
        return value


class WorkerRunResponse(BaseModel):
    status: str
    job_human_id: str | None = None
    message: str
    exit_code: int = 0


class DispatchRunResponse(BaseModel):
    mode: str
    message: str
    cancelled: bool
    completed: list[WorkerRunResponse]
    replayed: bool = False


class OrchestrationStatusResponse(BaseModel):
    project_human_id: str
    paused: bool
    paused_reason: str | None = None
    updated_at: str | None = None
    eligible_job_ids: list[str]


class RecoveryIdentityCheck(BaseModel):
    project_human_id: str
    ok: bool
    error: str | None = None


class RecoveryWorktreeAction(BaseModel):
    job_human_id: str
    action: str
    message: str


class RecoveryResponse(BaseModel):
    project_human_id: str
    dry_run: bool
    ok: bool
    expired_lease_job_ids: list[int]
    promoted_ready: list[str]
    blocked: list[str]
    identity_checks: list[RecoveryIdentityCheck]
    worktree_actions: list[RecoveryWorktreeAction]
    messages: list[str]
    replayed: bool = False


class DaemonStatusResponse(BaseModel):
    status: str
    pid: int | None = None
    heartbeat_at: str | None = None
    started_at: str | None = None
    last_error: str | None = None
    lock_path: str | None = None


class SchedulerEntryResponse(BaseModel):
    project_human_id: str
    enabled: bool
    paused: bool
    window_key: str
    due: bool
    cadence: str
    local_time: str


class SchedulerStatusResponse(BaseModel):
    daemon: DaemonStatusResponse
    schedules: list[SchedulerEntryResponse]


class ProjectionHealth(BaseModel):
    status: str
    enabled: bool
    paused: bool
    paused_reason: str | None = None
    reasons: list[str] = Field(default_factory=list)


class ProjectionJobItem(BaseModel):
    human_id: str
    queue: str
    role: str
    status: str
    outcome: str | None = None
    iteration_human_id: str | None = None
    work_item_human_id: str | None = None
    attempt: int
    max_attempts: int
    has_candidate: bool
    last_error: str | None = None


class ProjectionJobs(BaseModel):
    counts: dict[str, int]
    eligible_count: int
    items: list[ProjectionJobItem]


class ProjectionAssuranceItem(BaseModel):
    assurance_role: str
    result: str
    delivery_job_human_id: str | None = None
    assurance_job_human_id: str | None = None
    has_candidate: bool = False
    defect_human_id: str | None = None


class ProjectionAssurance(BaseModel):
    required_roles: list[str]
    role_results: dict[str, str]
    pending_count: int
    passed_count: int
    failed_count: int
    stale_count: int
    open_assurance_jobs: int
    items: list[ProjectionAssuranceItem]


class ProjectionDefect(BaseModel):
    defect_human_id: str
    assurance_role: str | None = None
    delivery_job_human_id: str | None = None
    result: str | None = None


class ProjectionIntegrationRun(BaseModel):
    iteration_human_id: str | None = None
    status: str
    integrated_sha: str | None = None
    source_job_human_ids: list[str] = Field(default_factory=list)
    source_sha_count: int = 0
    conflict_count: int = 0
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ProjectionIntegration(BaseModel):
    latest: ProjectionIntegrationRun | None = None
    job_counts: dict[str, int]
    run_count: int


class ProjectionRelease(BaseModel):
    latest_job_human_id: str | None = None
    status: str | None = None
    outcome: str | None = None
    gate: str | None = None
    job_counts: dict[str, int]


class ProjectionIssue(BaseModel):
    kind: str
    message: str
    job_human_id: str | None = None
    status: str | None = None


class ProjectionAgentRun(BaseModel):
    job_human_id: str
    role: str
    exit_code: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    has_candidate: bool = False
    created_at: str | None = None


class ProjectionUsage(BaseModel):
    reported: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    runs_with_usage: int
    run_count: int


class ProjectionLearning(BaseModel):
    agent_runs: list[ProjectionAgentRun]
    event_count: int
    usage: ProjectionUsage


class ProjectionInvalidation(BaseModel):
    delivery_job_human_id: str
    rework_job_human_id: str | None = None
    invalidated_candidate_sha: str | None = None
    reason: str
    created_at: str


class ProjectionApprovals(BaseModel):
    has_accepted_plan: bool
    sponsor_authority: str | None = None
    sponsor_granted: bool
    iteration_human_id: str | None = None
    release_gate: str | None = None
    open_pm_jobs: int


class ProjectionEvent(BaseModel):
    job_human_id: str
    event_type: str
    status: str | None = None
    message: str | None = None
    created_at: str


class ProjectionResponse(BaseModel):
    """Stable UI/Slack snapshot. No orchestration table or filesystem fields."""

    schema_version: int
    generated_at: str
    revision: str
    poll_after_seconds: int
    project_human_id: str
    headline: str
    health: ProjectionHealth
    jobs: ProjectionJobs
    assurance: ProjectionAssurance
    defects: list[ProjectionDefect]
    integration: ProjectionIntegration
    release: ProjectionRelease
    errors: list[ProjectionIssue]
    recoverable: list[ProjectionIssue]
    learning: ProjectionLearning
    approvals: ProjectionApprovals
    invalidations: list[ProjectionInvalidation]
    events: list[ProjectionEvent]
