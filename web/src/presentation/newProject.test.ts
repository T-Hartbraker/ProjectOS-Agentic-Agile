import { describe, expect, it } from "vitest";
import { isAbsoluteRepositoryPath, validateNewProjectForm } from "./newProject";

describe("new project validation", () => {
  it("requires a name and an absolute repository path", () => {
    expect(
      validateNewProjectForm({
        name: "",
        repositoryPath: "relative/path",
        objective: "",
      }),
    ).toEqual([
      "Project name is required.",
      "Repository path must be an absolute filesystem path.",
    ]);
  });

  it("accepts a Windows absolute git path", () => {
    expect(isAbsoluteRepositoryPath("C:\\dev\\delivery-repo")).toBe(true);
    expect(
      validateNewProjectForm({
        name: "Atlas",
        repositoryPath: "C:\\dev\\delivery-repo",
        objective: "Ship isolation",
      }),
    ).toEqual([]);
  });

  it("rejects path traversal", () => {
    expect(isAbsoluteRepositoryPath("C:\\dev\\..\\other")).toBe(false);
  });
});
