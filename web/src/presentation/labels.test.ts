import { describe, expect, it } from "vitest";
import { healthLabel, queueLabel, roleLabel, statusLabel, statusTone } from "./labels";

describe("operator-readable labels", () => {
  it("maps queues, roles, and statuses without dropping canonical keys", () => {
    expect(queueLabel("ASSURANCE_INTEGRATION")).toBe("Integration review");
    expect(queueLabel("ASSURANCE_SECURITY")).toBe("Security review");
    expect(roleLabel("ASSURANCE_QUALITY")).toBe("Quality reviewer");
    expect(statusLabel("RUNNING")).toBe("In progress");
    expect(statusLabel("SUCCEEDED")).toBe("Finished");
    expect(healthLabel("healthy")).toBe("Healthy");
  });

  it("keeps unknown tokens readable instead of hiding them", () => {
    expect(queueLabel("CUSTOM_QUEUE")).toBe("Custom Queue");
    expect(statusTone("FAILED")).toBe("bad");
    expect(statusTone("RUNNING")).toBe("warn");
  });
});
