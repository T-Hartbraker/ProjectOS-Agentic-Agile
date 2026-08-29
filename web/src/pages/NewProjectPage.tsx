import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api } from "../api/client";
import { validateNewProjectForm } from "../presentation/newProject";

const ONBOARDING_KEY = "projectos.onboardingPrompt";

export function NewProjectPage() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [repositoryPath, setRepositoryPath] = useState("");
  const [objective, setObjective] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const nextErrors = validateNewProjectForm({ name, repositoryPath, objective });
    setErrors(nextErrors);
    if (nextErrors.length > 0) {
      return;
    }
    setBusy(true);
    try {
      const created = await api.registerProject({ repository_path: repositoryPath.trim() });
      if (name.trim() && created.project_name && created.project_name !== name.trim()) {
        window.sessionStorage.setItem(
          ONBOARDING_KEY,
          `Registered ${created.project_human_id} (${created.project_name}). The repository identity name differs from the name you entered.`,
        );
      } else {
        window.sessionStorage.setItem(
          ONBOARDING_KEY,
          objective.trim() ||
            `Describe the first piece of work for ${created.project_name ?? created.project_human_id}.`,
        );
      }
      navigate(`/projects/${encodeURIComponent(created.project_human_id)}/intake`);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Could not register the project";
      setErrors([message]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page">
      <h1>New project</h1>
      <p className="muted">
        Point ProjectOS at an existing delivery repository. Identity and isolation still come from
        that repository. This form does not edit the registry file directly.
      </p>
      <form className="intake-form" onSubmit={(event) => void onSubmit(event)}>
        <label>
          Project name
          <input value={name} onChange={(event) => setName(event.target.value)} required />
        </label>
        <label>
          Repository path
          <input
            value={repositoryPath}
            onChange={(event) => setRepositoryPath(event.target.value)}
            placeholder="C:\dev\my-delivery-repo"
            required
          />
        </label>
        <label>
          Objective
          <textarea
            value={objective}
            onChange={(event) => setObjective(event.target.value)}
            placeholder="What should the first iteration accomplish?"
          />
        </label>
        {errors.length > 0 ? (
          <div className="banner error">
            {errors.map((item) => (
              <p key={item}>{item}</p>
            ))}
          </div>
        ) : null}
        <div className="intake-actions">
          <button type="submit" disabled={busy}>
            {busy ? "Registering…" : "Register project"}
          </button>
        </div>
      </form>
    </section>
  );
}

export function consumeOnboardingPrompt(): string | null {
  const value = window.sessionStorage.getItem(ONBOARDING_KEY);
  if (value) {
    window.sessionStorage.removeItem(ONBOARDING_KEY);
  }
  return value;
}
