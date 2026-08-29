import { describe, expect, it } from "vitest";
import type { JobResponse } from "../api/types";
import { describeActiveWork, runningJobs } from "./activeWork";

function job(overrides: Partial<JobResponse> = {}): JobResponse {
  return {
    human_id: "JOB-1",
    project_human_id: "PRJ-A",
    queue: "INTEGRATION",
    agent_role: "INTEGRATION",
    status: "RUNNING",
    lane: "delivery",
    iteration_human_id: "ITER-001",
    work_item_type: "objective",
    work_item_human_id: "WI-9",
    outcome: null,
    last_error: null,
    attempt: 1,
    max_attempts: 3,
    created_at: null,
    ready_at: null,
    started_at: new Date(Date.now() - 11 * 60_000).toISOString(),
    completed_at: null,
    updated_at: null,
    candidate_git_sha: "abc123def4567890",
    evidence_ref: null,
    depends_on: [],
    ...overrides,
  };
}

describe("active work narrative", () => {
  it("explains a running integration job instead of exposing RUNNING", () => {
    const described = describeActiveWork(job());
    expect(described.title).toBe("Integration");
    expect(described.status).toBe("In progress");
    expect(described.sentence).toMatch(/Combining the approved implementation/);
    expect(described.next).toMatch(/Release verification/);
    expect(described.sentence).not.toContain("RUNNING");
    expect(described.sentence).toContain("WI-9");
  });

  it("selects only in-progress jobs", () => {
    const running = runningJobs([
      job({ human_id: "JOB-RUN", status: "RUNNING" }),
      job({ human_id: "JOB-DONE", status: "SUCCEEDED", queue: "DELIVERY" }),
    ]);
    expect(running.map((item) => item.human_id)).toEqual(["JOB-RUN"]);
  });
});
