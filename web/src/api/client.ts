import type {
  ActivityResponse,
  CurrentStateResponse,
  DaemonStatusResponse,
  HealthResponse,
  IntakeResponse,
  JobGraphResponse,
  JobListResponse,
  OnboardingResponse,
  ProjectListResponse,
  ProjectResponse,
  ProjectSummaryResponse,
  ProjectionResponse,
  QualityResponse,
  ReleaseDetailResponse,
  ReleaseListResponse,
  ReportCatalogResponse,
  ReportDashboardResponse,
  ReportResponse,
  ReportSnapshotSummary,
  LearningResponse,
  MemoryAdminResponse,
  DecisionListResponse,
  DecisionResponse,
  SlackBinding,
  SlackBindingListResponse,
  SlackStatusResponse,
  SlackSettingsResponse,
  SlackSettingsUpdateRequest,
  SlackTokensUpdateRequest,
  SlackTestResponse,
  SlackTokensUpdateResponse,
  OpenAISettingsResponse,
  OpenAISettingsUpdateRequest,
  OpenAISecretPutRequest,
  OpenAISecretPutResponse,
  OpenAITestResponse,
  SlackNotifyResponse,
  SlackNotificationListResponse,
  AuditResponse,
  PortfolioResponse,
  SchedulerStatusResponse,
  WorkRequestRequest,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string;

  constructor(status: number, code: string, message: string, correlationId: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
  }
}

export function apiBase(): string {
  const configured = import.meta.env.VITE_API_BASE;
  if (configured === undefined || configured === "") {
    return "";
  }
  return configured.replace(/\/$/, "");
}

export function apiUrl(path: string): string {
  if (!path.startsWith("/")) {
    throw new Error("API paths must be absolute from the server root");
  }
  return `${apiBase()}${path}`;
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method: "GET",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
  return (await response.json()) as T;
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method: "PUT",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
  return (await response.json()) as T;
}

export async function apiDelete<T>(path: string): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method: "DELETE",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
  return (await response.json()) as T;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw await errorFromResponse(response);
  }
  return (await response.json()) as T;
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  const correlation =
    response.headers.get("x-correlation-id") ||
    response.headers.get("x-request-id") ||
    "";
  try {
    const body = (await response.json()) as {
      error?: { code?: string; message?: string; correlation_id?: string };
    };
    const err = body.error;
    if (err?.message) {
      return new ApiError(
        response.status,
        err.code || "http_error",
        err.message,
        err.correlation_id || correlation,
      );
    }
  } catch {
    // Fall through to generic HTTP error.
  }
  return new ApiError(
    response.status,
    "http_error",
    `Request failed (${response.status})`,
    correlation,
  );
}

export const api = {
  health: () => apiGet<HealthResponse>("/v1/health"),
  daemon: () => apiGet<DaemonStatusResponse>("/v1/daemon"),
  listProjects: () => apiGet<ProjectListResponse>("/v1/projects"),
  registerProject: (body: { repository_path: string }) =>
    apiPost<OnboardingResponse>("/v1/projects", body),
  getProject: (projectHumanId: string) =>
    apiGet<ProjectResponse>(`/v1/projects/${encodeURIComponent(projectHumanId)}`),
  projectSummary: (projectHumanId: string) =>
    apiGet<ProjectSummaryResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/summary`,
    ),
  projectCurrent: (projectHumanId: string) =>
    apiGet<CurrentStateResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/current`,
    ),
  projectProjection: (projectHumanId: string) =>
    apiGet<ProjectionResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/projection`,
    ),
  scheduler: () => apiGet<SchedulerStatusResponse>("/v1/scheduler"),
  previewWorkRequest: (projectHumanId: string, body: WorkRequestRequest) =>
    apiPost<IntakeResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/work-requests/preview`,
      body,
    ),
  submitWorkRequest: (projectHumanId: string, body: WorkRequestRequest) =>
    apiPost<IntakeResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/work-requests/submit`,
      body,
    ),
  projectJobs: (projectHumanId: string) =>
    apiGet<JobListResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/jobs`,
    ),
  projectGraph: (projectHumanId: string) =>
    apiGet<JobGraphResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/graph`,
    ),
  projectActivity: (projectHumanId: string) =>
    apiGet<ActivityResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/activity`,
    ),
  projectLearning: (projectHumanId: string) =>
    apiGet<LearningResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/learning`,
    ),
  retireMemory: (
    projectHumanId: string,
    memoryHumanId: string,
    body: { confirmed: boolean; reason: string; actor: string },
  ) =>
    apiPost<MemoryAdminResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/learning/memories/${encodeURIComponent(memoryHumanId)}/retire`,
      body,
    ),
  supersedeMemory: (
    projectHumanId: string,
    memoryHumanId: string,
    body: {
      confirmed: boolean;
      reason: string;
      actor: string;
      successor_title: string;
      evidence_ref?: string | null;
    },
  ) =>
    apiPost<MemoryAdminResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/learning/memories/${encodeURIComponent(memoryHumanId)}/supersede`,
      body,
    ),
  projectQuality: (projectHumanId: string) =>
    apiGet<QualityResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/quality`,
    ),
  projectReleases: (projectHumanId: string) =>
    apiGet<ReleaseListResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/releases`,
    ),
  projectRelease: (projectHumanId: string, releaseHumanId: string) =>
    apiGet<ReleaseDetailResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/releases/${encodeURIComponent(releaseHumanId)}`,
    ),
  releaseArtifactUrl: (
    projectHumanId: string,
    releaseHumanId: string,
    artifactHumanId: string,
  ) =>
    apiUrl(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/releases/${encodeURIComponent(releaseHumanId)}/artifacts/${encodeURIComponent(artifactHumanId)}`,
    ),
  projectReports: (projectHumanId: string) =>
    apiGet<ReportCatalogResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/reports`,
    ),
  projectReportDashboard: (projectHumanId: string) =>
    apiGet<ReportDashboardResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/reports/dashboard`,
    ),
  saveProjectReportSnapshot: (projectHumanId: string, kind: string) =>
    apiPost<ReportSnapshotSummary>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/reports/${encodeURIComponent(kind)}/snapshots`,
      {},
    ),
  projectReportSnapshot: (projectHumanId: string, snapshotHumanId: string) =>
    apiGet<ReportResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/reports/snapshots/${encodeURIComponent(snapshotHumanId)}`,
    ),
  projectReportSnapshotDownloadUrl: (
    projectHumanId: string,
    snapshotHumanId: string,
    format: string,
  ) =>
    apiUrl(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/reports/snapshots/${encodeURIComponent(snapshotHumanId)}/download?format=${encodeURIComponent(format)}`,
    ),
  projectReport: (projectHumanId: string, kind: string, iterationHumanId?: string) => {
    const query = iterationHumanId
      ? `?iteration_human_id=${encodeURIComponent(iterationHumanId)}`
      : "";
    return apiGet<ReportResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/reports/${encodeURIComponent(kind)}${query}`,
    );
  },
  projectReportDownloadUrl: (
    projectHumanId: string,
    kind: string,
    format: string,
    iterationHumanId?: string,
  ) => {
    const params = new URLSearchParams({ format });
    if (iterationHumanId) {
      params.set("iteration_human_id", iterationHumanId);
    }
    return apiUrl(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/reports/${encodeURIComponent(kind)}/download?${params.toString()}`,
    );
  },
  projectDecisions: (projectHumanId: string) =>
    apiGet<DecisionListResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/decisions`,
    ),
  openDecision: (
    projectHumanId: string,
    body: {
      action: string;
      reason: string;
      impact: string;
      requested_by: string;
      target_kind?: string;
      target_human_id?: string | null;
    },
  ) =>
    apiPost<DecisionResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/decisions`,
      body,
    ),
  approveDecision: (
    projectHumanId: string,
    decisionHumanId: string,
    body: { confirmed: boolean; actor: string; reason: string },
  ) =>
    apiPost<DecisionResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/decisions/${encodeURIComponent(decisionHumanId)}/approve`,
      body,
    ),
  rejectDecision: (
    projectHumanId: string,
    decisionHumanId: string,
    body: { confirmed: boolean; actor: string; reason: string },
  ) =>
    apiPost<DecisionResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/decisions/${encodeURIComponent(decisionHumanId)}/reject`,
      body,
    ),
  slackStatus: () => apiGet<SlackStatusResponse>("/v1/integrations/slack/status"),
  slackSettings: () => apiGet<SlackSettingsResponse>("/v1/settings/integrations/slack"),
  updateSlackSettings: (body: SlackSettingsUpdateRequest) =>
    apiPut<SlackSettingsResponse>("/v1/settings/integrations/slack", body),
  saveSlackTokens: (body: SlackTokensUpdateRequest) =>
    apiPost<SlackTokensUpdateResponse>("/v1/settings/integrations/slack/tokens", body),
  testSlackConnection: () => apiPost<SlackTestResponse>("/v1/settings/integrations/slack/test", {}),
  openaiSettings: () => apiGet<OpenAISettingsResponse>("/v1/settings/integrations/openai"),
  updateOpenAISettings: (body: OpenAISettingsUpdateRequest) =>
    apiPut<OpenAISettingsResponse>("/v1/settings/integrations/openai", body),
  saveOpenAISecret: (body: OpenAISecretPutRequest) =>
    apiPut<OpenAISecretPutResponse>("/v1/settings/integrations/openai/secret", body),
  removeOpenAISecret: () => apiDelete<OpenAISecretPutResponse>("/v1/settings/integrations/openai/secret"),
  testOpenAIConnection: () => apiPost<OpenAITestResponse>("/v1/settings/integrations/openai/test", {}),
  projectSlack: (projectHumanId: string) =>
    apiGet<SlackBindingListResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/integrations/slack`,
    ),
  bindSlack: (
    projectHumanId: string,
    body: { channel_id: string; team_id?: string | null; thread_ts?: string | null },
  ) =>
    apiPost<SlackBinding>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/integrations/slack/bind`,
      body,
    ),
  unbindSlack: (
    projectHumanId: string,
    body: { channel_id: string; team_id?: string | null; thread_ts?: string | null },
  ) =>
    apiPost<{ ok: boolean; project_human_id: string; binding_human_id: string }>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/integrations/slack/unbind`,
      body,
    ),
  notifySlack: (projectHumanId: string) =>
    apiPost<SlackNotifyResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/integrations/slack/notify`,
      {},
    ),
  projectSlackNotifications: (projectHumanId: string) =>
    apiGet<SlackNotificationListResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/integrations/slack/notifications`,
    ),
  projectAudit: (
    projectHumanId: string,
    params?: Record<string, string | undefined>,
  ) => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params || {})) {
      if (value) {
        search.set(key, value);
      }
    }
    const query = search.toString();
    const suffix = query ? `?${query}` : "";
    return apiGet<AuditResponse>(
      `/v1/projects/${encodeURIComponent(projectHumanId)}/audit${suffix}`,
    );
  },
  portfolio: () => apiGet<PortfolioResponse>("/v1/portfolio"),
};
