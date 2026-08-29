export function isAbsoluteRepositoryPath(value: string): boolean {
  const text = value.trim();
  if (!text) {
    return false;
  }
  if (text.includes("..")) {
    return false;
  }
  return /^[A-Za-z]:[\\/]/.test(text) || text.startsWith("/");
}

export function validateNewProjectForm(input: {
  name: string;
  repositoryPath: string;
  objective: string;
}): string[] {
  const errors: string[] = [];
  if (!input.name.trim()) {
    errors.push("Project name is required.");
  }
  if (!isAbsoluteRepositoryPath(input.repositoryPath)) {
    errors.push("Repository path must be an absolute filesystem path.");
  }
  if (input.objective.length > 2000) {
    errors.push("Objective is too long.");
  }
  return errors;
}
