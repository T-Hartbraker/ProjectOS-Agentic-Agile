export type HealthComponent = {
  name: string;
  status: string;
  required: boolean;
  detail: string;
  pid: number | null;
};

export type HealthResponse = {
  status: string;
  service: string;
  version: string;
  ready?: boolean;
  notice?: string | null;
  components?: HealthComponent[];
};

export type ProjectResponse = {
  project_human_id: string;
  repository_root: string;
  enabled: boolean;
};

export type ProjectListResponse = {
  projects: ProjectResponse[];
};

export type OnboardingResponse = ProjectResponse & {
  action: string;
  git_root: string;
  project_name: string | null;
};

export type ProjectSummaryResponse = {
  project_human_id: string;
  enabled: boolean;
  job_counts: Record<string, number>;
  current_iteration_human_id: string | null;
  current_release_job_human_id: string | null;
  current_release_status: string | null;
  has_accepted_plan: boolean;
};

export type CurrentStateResponse = {
  project_human_id: string;
  iteration_human_id: string | null;
  release_job_human_id: string | null;
  release_status: string | null;
  from_accepted_plan: boolean;
};

export type DaemonStatusResponse = {
  status: string;
  pid: number | null;
  heartbeat_at: string | null;
  started_at: string | null;
  last_error: string | null;
  lock_path: string | null;
};

export type SchedulerEntryResponse = {
  project_human_id: string;
  enabled: boolean;
  paused: boolean;
  window_key: string;
  due: boolean;
  cadence: string;
  local_time: string;
};

export type SchedulerStatusResponse = {
  daemon: DaemonStatusResponse;
  schedules: SchedulerEntryResponse[];
};

export type ProjectionHealth = {
  status: string;
  enabled: boolean;
  paused: boolean;
  paused_reason: string | null;
  reasons: string[];
};

export type ProjectionJobItem = {
  human_id: string;
  queue: string;
  role: string;
  status: string;
  outcome: string | null;
  iteration_human_id: string | null;
  work_item_human_id: string | null;
  attempt: number;
  max_attempts: number;
  has_candidate: boolean;
  last_error: string | null;
};

export type ProjectionAssuranceItem = {
  assurance_role: string;
  result: string;
  delivery_job_human_id: string | null;
  assurance_job_human_id: string | null;
  has_candidate: boolean;
  defect_human_id: string | null;
};

export type ProjectionDefect = {
  defect_human_id: string;
  assurance_role: string | null;
  delivery_job_human_id: string | null;
  result: string | null;
};

export type ProjectionIntegrationRun = {
  iteration_human_id: string | null;
  status: string;
  integrated_sha: string | null;
  source_job_human_ids: string[];
  source_sha_count: number;
  conflict_count: number;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type ProjectionIssue = {
  kind: string;
  message: string;
  job_human_id: string | null;
  status: string | null;
};

export type ProjectionUsage = {
  reported: boolean;
  input_tokens: number | null;
  output_tokens: number | null;
  runs_with_usage: number;
  run_count: number;
};

export type ProjectionInvalidation = {
  delivery_job_human_id: string;
  rework_job_human_id: string | null;
  invalidated_candidate_sha: string | null;
  reason: string;
  created_at: string;
};

export type WorkRequestRequest = {
  business_request: string;
  objective: string;
  acceptance: string;
  iteration_human_id?: string | null;
  sponsor_authority?: string | null;
};

export type IntakeAssumption = {
  code: string;
  statement: string;
  owner: string;
};

export type IntakeDecisionRequest = {
  code: string;
  question: string;
  reserved_for: string;
};

export type IntakeExpectedJob = {
  human_id: string;
  queue: string | null;
  agent_role: string | null;
  depends_on: string[];
};

export type IntakeResponse = {
  status: string;
  project_human_id: string;
  dry_run: boolean;
  assumptions: IntakeAssumption[];
  decision_requests: IntakeDecisionRequest[];
  expected_jobs: IntakeExpectedJob[];
  plan: Record<string, unknown> | null;
  plan_source: string | null;
  jobs_created: string[];
  error: string | null;
};

export type ProjectionEvent = {
  job_human_id: string;
  event_type: string;
  status: string | null;
  message: string | null;
  created_at: string;
};

export type ProjectionResponse = {
  schema_version: number;
  generated_at: string;
  revision: string;
  poll_after_seconds: number;
  project_human_id: string;
  headline: string;
  health: ProjectionHealth;
  jobs: {
    counts: Record<string, number>;
    eligible_count: number;
    items: ProjectionJobItem[];
  };
  assurance: {
    required_roles: string[];
    role_results: Record<string, string>;
    pending_count: number;
    passed_count: number;
    failed_count: number;
    stale_count: number;
    open_assurance_jobs: number;
    items: ProjectionAssuranceItem[];
  };
  defects: ProjectionDefect[];
  integration: {
    latest: ProjectionIntegrationRun | null;
    job_counts: Record<string, number>;
    run_count: number;
  };
  release: {
    latest_job_human_id: string | null;
    status: string | null;
    outcome: string | null;
    gate: string | null;
    job_counts: Record<string, number>;
  };
  errors: ProjectionIssue[];
  recoverable: ProjectionIssue[];
  learning: {
    agent_runs: Array<{
      job_human_id: string;
      role: string;
      exit_code: number | null;
      duration_ms: number | null;
      error: string | null;
      has_candidate: boolean;
      created_at: string | null;
    }>;
    event_count: number;
    usage: ProjectionUsage;
  };
  approvals: {
    has_accepted_plan: boolean;
    sponsor_authority: string | null;
    sponsor_granted: boolean;
    iteration_human_id: string | null;
    release_gate: string | null;
    open_pm_jobs: number;
  };
  invalidations: ProjectionInvalidation[];
  events: ProjectionEvent[];
};

export type JobPresentation = {
  queue_label: string;
  role_label: string;
  status_label: string;
  lane_label: string;
  activity: string;
  next_step: string;
};

export type JobResponse = {
  human_id: string;
  project_human_id: string;
  queue: string;
  agent_role: string;
  status: string;
  lane: string;
  iteration_human_id: string | null;
  work_item_type: string | null;
  work_item_human_id: string | null;
  outcome: string | null;
  last_error: string | null;
  attempt: number;
  max_attempts: number;
  created_at: string | null;
  ready_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
  candidate_git_sha: string | null;
  evidence_ref: string | null;
  depends_on: string[];
  presentation?: JobPresentation | null;
};

export type JobListResponse = {
  jobs: JobResponse[];
};

export type JobGraphResponse = {
  nodes: JobResponse[];
  edges: Array<{ job_human_id: string; depends_on: string }>;
};

export type AgentRunResponse = {
  job_human_id: string;
  queue: string;
  role: string;
  lane: string;
  job_status: string;
  exit_code: number | null;
  duration_ms: number | null;
  error: string | null;
  started_at: string | null;
  ended_at: string | null;
  created_at: string | null;
  candidate_git_sha: string | null;
  has_candidate: boolean;
  evidence_ref: string | null;
  prompt_ref: string | null;
};

export type ActivityResponse = {
  in_flight: JobResponse[];
  recent_runs: AgentRunResponse[];
  recent_events: Array<{
    id: number;
    job_human_id: string;
    event_type: string;
    status: string | null;
    message: string | null;
    created_at: string;
  }>;
};

export type QualityEvidenceItem = {
  assurance_role: string;
  result: string;
  candidate_git_sha: string | null;
  evidence_ref: string | null;
  delivery_job_human_id: string | null;
  assurance_job_human_id: string | null;
  assurance_job_status: string | null;
  defect_human_id: string | null;
  created_at: string | null;
};

export type QualityFinding = {
  role: string;
  result: string;
  candidate_git_sha: string | null;
  evidence_ref: string | null;
  job_human_id: string | null;
};

export type QualityDefect = {
  defect_human_id: string;
  severity: string;
  priority: string;
  status: string;
  assurance_role: string | null;
  delivery_job_human_id: string | null;
  candidate_git_sha: string | null;
  result: string | null;
};

export type QualityLineageItem = {
  kind: string;
  delivery_job_human_id: string | null;
  assurance_job_human_id: string | null;
  rework_job_human_id: string | null;
  retest_job_human_id: string | null;
  candidate_git_sha: string | null;
  invalidated_candidate_sha: string | null;
  reason: string | null;
  status: string | null;
};

export type QualityResponse = {
  project_human_id: string;
  qa_pass_authority: string;
  developer_can_mark_qa_passed: boolean;
  summary: {
    required_roles: string[];
    role_results: Record<string, string>;
    pending_count: number;
    passed_count: number;
    failed_count: number;
    stale_count: number;
    open_assurance_jobs: number;
    evaluated_candidate_shas: string[];
  };
  evidence: QualityEvidenceItem[];
  findings: {
    security: QualityFinding | null;
    quality: QualityFinding | null;
  };
  defects: QualityDefect[];
  defect_counts: {
    by_severity: Record<string, number>;
    by_priority: Record<string, number>;
    by_status: Record<string, number>;
  };
  lineage: QualityLineageItem[];
  release_blocking_reasons: string[];
};

export type ReleaseArtifactRef = {
  artifact_human_id: string;
  filename: string;
  sha256: string;
  byte_size: number;
  media_type: string;
  kind: string;
  download_ref: string;
};

export type ReleaseSummary = {
  release_human_id: string;
  job_human_id: string;
  iteration_human_id: string | null;
  status: string;
  outcome: string | null;
  gate: string;
  integrated_sha: string | null;
  released_sha: string | null;
  qa_recommendation: string;
  artifact_count: number;
  updated_at: string | null;
};

export type ReleaseListResponse = {
  project_human_id: string;
  releases: ReleaseSummary[];
};

export type ReleaseDetailResponse = ReleaseSummary & {
  project_human_id: string;
  qa_recommendation_detail: { status: string; reasons: string[] };
  known_findings: Array<{
    kind: string;
    result: string | null;
    role: string | null;
    candidate_git_sha: string | null;
    evidence_ref: string | null;
  }>;
  release_notes: string | null;
  migration_notes: string | null;
  rollback_notes: string | null;
  manifest: {
    release_human_id: string;
    integrated_sha: string | null;
    released_sha: string | null;
    files: Array<{
      artifact_human_id: string;
      filename: string;
      sha256: string;
      byte_size: number;
    }>;
  };
  checksums: Array<{ filename: string; sha256: string }>;
  artifacts: ReleaseArtifactRef[];
  last_error: string | null;
};

export type ReportSource = {
  entity_type: string;
  entity_human_id: string;
  timestamp: string | null;
};

export type ReportCatalogItem = {
  kind: string;
  title: string;
};

export type ReportCatalogResponse = {
  project_human_id: string;
  download_formats: string[];
  reports: ReportCatalogItem[];
};

export type ReportResponse = {
  schema_version: number;
  report_kind: string;
  title: string;
  project_human_id: string;
  iteration_human_id: string | null;
  release_human_id: string | null;
  generated_at: string;
  revision: string;
  origin: string;
  snapshot_human_id: string | null;
  saved_at: string | null;
  sources: ReportSource[];
  body: Record<string, unknown>;
};

export type ReportSnapshotSummary = {
  snapshot_human_id: string;
  project_human_id: string;
  report_kind: string;
  revision: string;
  iteration_human_id: string | null;
  release_human_id: string | null;
  generated_at: string;
  saved_at: string;
  origin: string;
};

export type LearningMemory = {
  memory_human_id: string;
  project_human_id: string;
  agent_role: string;
  memory_kind: string;
  title: string;
  evidence_ref: string | null;
  source_job_human_id: string | null;
  confidence: number;
  occurrence_count: number;
  last_validated_at: string | null;
  status: string;
  promotion_mode: string;
  rejection_code: string | null;
  rejection_reason: string | null;
  superseded_by_memory_human_id: string | null;
  created_at: string;
  updated_at: string;
};

export type LearningEvent = {
  project_human_id: string;
  memory_human_id: string;
  event_type: string;
  job_human_id: string | null;
  actor: string | null;
  rejection_code: string | null;
  rejection_reason: string | null;
  created_at: string;
};

export type LearningInjection = {
  project_human_id: string;
  memory_human_id: string;
  job_human_id: string;
  agent_run_id: number | null;
  created_at: string;
};

export type LearningResponse = {
  project_human_id: string;
  notice: string;
  active_memories: LearningMemory[];
  rejected_memories: LearningMemory[];
  retired_memories: LearningMemory[];
  superseded_memories: LearningMemory[];
  events: LearningEvent[];
  injected_in_recent_runs: LearningInjection[];
};

export type MemoryAdminResponse = {
  project_human_id: string;
  action: string;
  actor: string;
  reason: string;
  memory: LearningMemory;
  successor: LearningMemory | null;
};

export type DecisionEvent = {
  project_human_id: string;
  decision_human_id: string;
  event_type: string;
  actor: string | null;
  reason: string | null;
  created_at: string;
};

export type DecisionResponse = {
  decision_human_id: string;
  project_human_id: string;
  action: string;
  target_kind: string;
  target_human_id: string | null;
  reason: string;
  impact: string;
  requested_by: string;
  status: string;
  decided_by: string | null;
  decision_reason: string | null;
  execution_result: string | null;
  created_at: string;
  updated_at: string;
  decided_at: string | null;
  notice: string;
  events: DecisionEvent[];
};

export type DecisionListResponse = {
  project_human_id: string;
  notice: string;
  decisions: DecisionResponse[];
};

export type SlackBinding = {
  binding_human_id: string;
  project_human_id: string;
  team_id: string | null;
  channel_id: string;
  thread_ts: string | null;
  created_at: string;
  updated_at: string;
  repository_root?: string | null;
  repository_source?: string | null;
  notice?: string | null;
};

export type SlackMessageRef = {
  project_human_id: string;
  team_id: string | null;
  channel_id: string;
  thread_ts: string | null;
  message_ts: string;
  created_at: string | null;
};

export type SlackBindingListResponse = {
  project_human_id: string;
  notice: string;
  repository_root: string;
  repository_source: string;
  bindings: SlackBinding[];
  message_refs: SlackMessageRef[];
};

export type SlackBoundChannel = {
  project_human_id: string;
  channel_id: string;
  team_id: string | null;
  thread_ts: string | null;
  channel_name: string | null;
};

export type SlackStatusResponse = {
  mode: string;
  connection_status: string;
  app_token: string;
  bot_token: string;
  workspace_name: string | null;
  team_id: string | null;
  bound_channel: SlackBoundChannel | null;
  bound_channels: SlackBoundChannel[];
  interface_channels: SlackInterfaceChannel[];
  default_channel_id: string | null;
  setup_steps: string[];
  detail: string;
};

export type SlackInterfaceChannel = {
  channel_id: string;
  team_id: string | null;
  is_default: boolean;
};

export type SlackSettingsResponse = {
  enabled: boolean;
  mode: string;
  transport: string;
  connection_status: string;
  detail: string;
  workspace_name: string | null;
  team_id: string | null;
  connection_updated_at: string | null;
  app_token: string;
  bot_token: string;
  app_token_present: boolean;
  bot_token_present: boolean;
  app_token_valid_prefix: boolean;
  bot_token_valid_prefix: boolean;
  signing_secret_present: boolean;
  app_token_configured?: boolean;
  bot_token_configured?: boolean;
  app_token_source?: string;
  bot_token_source?: string;
  configured?: boolean;
  tokens_ready?: boolean;
  connection_state?: string;
  storage?: string;
  interface_channels: SlackInterfaceChannel[];
  default_channel_id: string | null;
  bound_channels: SlackBoundChannel[];
  setup_steps: string[];
};

export type SlackSettingsUpdateRequest = {
  add_interface_channels?: Array<{
    channel_id: string;
    team_id?: string | null;
    is_default?: boolean;
  }>;
  remove_interface_channels?: Array<{
    channel_id: string;
    team_id?: string | null;
  }>;
  default_channel_id?: string | null;
  default_team_id?: string | null;
};

export type SlackTokensUpdateRequest = {
  app_token?: string;
  bot_token?: string;
  signing_secret?: string;
};

export type SlackTokensUpdateResponse = {
  ok: boolean;
  updated_fields: string[];
  restart_required: boolean;
  notice: string;
  storage: string;
  app_token: string;
  bot_token: string;
  app_token_present: boolean;
  bot_token_present: boolean;
  app_token_valid_prefix: boolean;
  bot_token_valid_prefix: boolean;
  signing_secret_present: boolean;
};

export type SlackTestResponse = {
  ok: boolean;
  app_token: string;
  bot_token: string;
  socket_mode: string;
  workspace: string | null;
  team_id: string | null;
  detail: string;
};

export type OpenAISettingsResponse = {
  enabled: boolean;
  api_key_configured: boolean;
  api_key_source: "environment" | "encrypted_store" | "none";
  model: string;
  supported_models: string[];
  slack_chatgpt_user_id: string;
  slack_chatgpt_user_id_source: string;
  last_test_status: "success" | "failed" | "not_tested" | null;
  last_test_at: string | null;
  last_error: string | null;
  setup_steps: string[];
};

export type OpenAISettingsUpdateRequest = {
  model?: string;
  slack_chatgpt_user_id?: string;
};

export type OpenAISecretPutRequest = {
  api_key?: string;
};

export type OpenAISecretPutResponse = {
  ok: boolean;
  api_key_configured: boolean;
  api_key_source: string;
  notice: string;
};

export type OpenAITestResponse = {
  ok: boolean;
  detail: string | null;
  response_id: string | null;
};

export type SlackNotification = {
  notification_human_id: string;
  project_human_id: string;
  kind: string;
  entity_human_id: string;
  channel_id: string;
  team_id: string | null;
  thread_ts: string | null;
  text: string;
  dashboard_path: string;
  created_at: string;
};

export type SlackNotifyResponse = {
  project_human_id: string;
  posted: SlackNotification[];
  already_posted: string[];
  notice: string;
};

export type SlackNotificationListResponse = {
  project_human_id: string;
  notifications: SlackNotification[];
};

export type AuditEvent = {
  occurred_at: string;
  actor_type: string;
  actor_id: string;
  action: string;
  entity_kind: string;
  entity_human_id: string;
  iteration_human_id: string | null;
  source: string;
};

export type AuditResponse = {
  project_human_id: string;
  notice: string;
  events: AuditEvent[];
};

export type PortfolioProjectCard = {
  project_human_id: string;
  enabled: boolean;
  health: string;
  current_iteration_human_id: string | null;
  blocker_count: number;
  release_human_id: string | null;
  release_status: string | null;
  active_job_count: number;
  open_defect_count: number;
  active_memory_count: number;
};

export type PortfolioResponse = {
  notice: string;
  projects: PortfolioProjectCard[];
};

export type ReportDashboardResponse = {
  origin: string;
  project_human_id: string;
  generated_at: string;
  iteration_human_id: string | null;
  notice: string;
  download_formats: string[];
  reports: ReportResponse[];
  snapshots: ReportSnapshotSummary[];
};
