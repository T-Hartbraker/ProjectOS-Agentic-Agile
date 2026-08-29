import { describe, expect, it } from "vitest";
import type { ReportResponse } from "../api/types";
import { reportNarrative } from "./reportNarrative";

function report(kind: string, body: Record<string, unknown>): ReportResponse {
  return {
    schema_version: 1,
    report_kind: kind,
    title: kind,
    project_human_id: "PRJ-A",
    iteration_human_id: "ITER-001",
    release_human_id: null,
    generated_at: "2026-08-22T00:00:00Z",
    revision: "rev-1",
    origin: "live",
    snapshot_human_id: null,
    saved_at: null,
    sources: [],
    body,
  };
}

describe("report narrative", () => {
  it("renders operator sentences instead of raw event listings", () => {
    const sections = reportNarrative([
      report("project-status", { health: "healthy" }),
      report("quality", {
        summary: { passed_count: 4, failed_count: 0, pending_count: 0 },
        role_results: { ASSURANCE_SECURITY: "SUCCEEDED" },
      }),
      report("risks", { issue_count: 0 }),
    ]);
    const quality = sections.find((item) => item.id === "quality");
    expect(quality?.body).toMatch(/All 4 independent assurance checks passed/);
    expect(sections.map((item) => item.title)).toContain("Project status");
    expect(sections.map((item) => item.title)).toContain("Next actions");
    expect(JSON.stringify(sections)).not.toMatch(/run_event/);
  });
});
