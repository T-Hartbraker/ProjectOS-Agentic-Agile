import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, apiUrl } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("apiUrl", () => {
  it("keeps versioned HTTP paths and rejects relative paths", () => {
    expect(apiUrl("/v1/health")).toBe("/v1/health");
    expect(() => apiUrl("v1/health")).toThrow(/absolute/);
  });
});

describe("apiGet", () => {
  it("returns JSON for a healthy control-plane response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          status: "degraded",
          service: "projectos",
          version: "0.1.0",
          ready: false,
          notice: "daemon stopped",
          components: [{ name: "api", status: "ok", required: true, detail: "up", pid: null }],
        }),
      }),
    );
    const health = await api.health();
    expect(health.service).toBe("projectos");
    expect(health.status).toBe("degraded");
    expect(health.components?.[0]?.name).toBe("api");
  });

  it("maps structured API errors without treating them as filesystem failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        headers: new Headers({ "x-correlation-id": "cid-1" }),
        json: async () => ({
          error: {
            code: "not_found",
            message: "project 'PRJ-MISSING' is not in the registry",
            correlation_id: "cid-1",
          },
        }),
      }),
    );
    await expect(api.getProject("PRJ-MISSING")).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      code: "not_found",
      correlationId: "cid-1",
    } satisfies Partial<ApiError>);
  });
});

describe("projectQuality", () => {
  it("requests the versioned quality DTO", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ project_human_id: "PRJ-A", developer_can_mark_qa_passed: false }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.projectQuality("PRJ-A");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/projects/PRJ-A/quality",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("projectLearning", () => {
  it("requests the versioned learning DTO", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ project_human_id: "PRJ-A", active_memories: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.projectLearning("PRJ-A");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/projects/PRJ-A/learning",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("retireMemory", () => {
  it("posts retire through the versioned learning admin path", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ action: "retire" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.retireMemory("PRJ-A", "MEM-1", {
      confirmed: true,
      reason: "stale",
      actor: "operator",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/projects/PRJ-A/learning/memories/MEM-1/retire",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

describe("projectSlack", () => {
  it("requests the versioned Slack binding DTO", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ project_human_id: "PRJ-A", bindings: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.projectSlack("PRJ-A");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/projects/PRJ-A/integrations/slack",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("portfolio and audit", () => {
  it("requests the versioned portfolio DTO", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ notice: "n", projects: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.portfolio();
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/portfolio",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("requests the versioned audit DTO with filters", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ project_human_id: "PRJ-A", events: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.projectAudit("PRJ-A", { source: "slack" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/projects/PRJ-A/audit?source=slack",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("projectDecisions", () => {
  it("requests the versioned decisions DTO", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ project_human_id: "PRJ-A", decisions: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.projectDecisions("PRJ-A");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/projects/PRJ-A/decisions",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("projectReports", () => {
  it("requests the versioned report catalog", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ project_human_id: "PRJ-A", reports: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.projectReports("PRJ-A");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/projects/PRJ-A/reports",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("builds a versioned snapshot download URL", () => {
    expect(api.projectReportDownloadUrl("PRJ-A", "project-status", "html")).toBe(
      "/v1/projects/PRJ-A/reports/project-status/download?format=html",
    );
  });

  it("requests the live reports dashboard", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ origin: "live", reports: [], snapshots: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.projectReportDashboard("PRJ-A");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/projects/PRJ-A/reports/dashboard",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("projectReleases", () => {
  it("requests the versioned release list DTO", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ project_human_id: "PRJ-A", releases: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.projectReleases("PRJ-A");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/projects/PRJ-A/releases",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("registerProject", () => {
  it("posts an absolute repository_path to the governed projects API", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        action: "registered",
        project_human_id: "PRJ-NEW",
        repository_root: "C:\\dev\\repo",
        enabled: true,
        git_root: "C:\\dev\\repo",
        project_name: "Atlas",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const created = await api.registerProject({ repository_path: "C:\\dev\\repo" });
    expect(created.project_human_id).toBe("PRJ-NEW");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/projects",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ repository_path: "C:\\dev\\repo" }),
      }),
    );
  });
});

describe("project isolation", () => {
  it("scopes project reads by project_human_id and keeps portfolio separate", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ project_human_id: "PRJ-A", jobs: [], projects: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.projectJobs("PRJ-A");
    await api.projectJobs("PRJ-B");
    await api.portfolio();
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/v1/projects/PRJ-A/jobs",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/v1/projects/PRJ-B/jobs",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/v1/portfolio",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("projectProjection", () => {
  it("requests the versioned projection DTO", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ project_human_id: "PRJ-A" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.projectProjection("PRJ-A");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/projects/PRJ-A/projection",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("slackStatus", () => {
  it("requests the versioned Slack Socket Mode status DTO", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ mode: "socket", connection_status: "not_configured" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.slackStatus();
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/integrations/slack/status",
      expect.objectContaining({ method: "GET" }),
    );
  });
});
