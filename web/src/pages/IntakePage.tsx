import { FormEvent, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { IntakeResponse, WorkRequestRequest } from "../api/types";
import { ProjectNav } from "../components/ProjectNav";
import { consumeOnboardingPrompt } from "./NewProjectPage";
import { queueLabel } from "../presentation/labels";

const emptyForm = {
  business_request: "",
  objective: "",
  acceptance: "",
  iteration_human_id: "",
};

export function IntakePage() {
  const { projectHumanId } = useParams();
  const [form, setForm] = useState(emptyForm);
  const [preview, setPreview] = useState<IntakeResponse | null>(null);
  const [busy, setBusy] = useState<"preview" | "submit" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [onboarding, setOnboarding] = useState<string | null>(null);

  useEffect(() => {
    setOnboarding(consumeOnboardingPrompt());
  }, []);

  if (!projectHumanId) {
    return null;
  }
  const id = projectHumanId;

  function field(
    name: keyof typeof emptyForm,
    value: string,
  ) {
    setForm((current) => ({ ...current, [name]: value }));
    setPreview(null);
  }

  function payload(): WorkRequestRequest {
    return {
      business_request: form.business_request,
      objective: form.objective,
      acceptance: form.acceptance,
      iteration_human_id: form.iteration_human_id.trim() || null,
    };
  }

  async function onPreview(event: FormEvent) {
    event.preventDefault();
    setBusy("preview");
    setError(null);
    try {
      setPreview(await api.previewWorkRequest(id, payload()));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Preview failed");
    } finally {
      setBusy(null);
    }
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy("submit");
    setError(null);
    try {
      setPreview(await api.submitWorkRequest(id, payload()));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Submit failed");
    } finally {
      setBusy(null);
    }
  }

  const blocked =
    (preview?.decision_requests.length ?? 0) > 0 || preview?.status === "needs_sponsor_decision";

  return (
    <section className="page">
      <ProjectNav projectHumanId={id} />
      <h1>New Work</h1>
      {onboarding ? <p className="onboarding-prompt">{onboarding}</p> : null}
      <p className="muted">
        Describe the business request, objective, and acceptance. The PM keeps delegated
        technical authority over jobs, queues, and implementation. Do not provide those here.
      </p>
      <form className="intake-form" onSubmit={onPreview}>
        <label>
          Business request
          <textarea
            required
            rows={4}
            value={form.business_request}
            onChange={(event) => field("business_request", event.target.value)}
          />
        </label>
        <label>
          Objective
          <textarea
            required
            rows={3}
            value={form.objective}
            onChange={(event) => field("objective", event.target.value)}
          />
        </label>
        <label>
          Acceptance
          <textarea
            required
            rows={4}
            value={form.acceptance}
            onChange={(event) => field("acceptance", event.target.value)}
          />
        </label>
        <label>
          Iteration (optional)
          <input
            value={form.iteration_human_id}
            onChange={(event) => field("iteration_human_id", event.target.value)}
          />
        </label>
        {error ? <div className="banner error">{error}</div> : null}
        <div className="intake-actions">
          <button type="submit" disabled={busy !== null}>
            {busy === "preview" ? "Previewing…" : "Preview plan"}
          </button>
          <button
            type="button"
            disabled={busy !== null || !preview || blocked}
            onClick={(event) => void onSubmit(event)}
          >
            {busy === "submit" ? "Submitting…" : "Submit to PM"}
          </button>
        </div>
      </form>

      {preview ? (
        <div className="intake-preview">
          <p className="stat">{preview.status}</p>
          {preview.error ? <p className="muted">{preview.error}</p> : null}

          {preview.decision_requests.length > 0 ? (
            <article className="card">
              <h2>Sponsor decision required</h2>
              <ul className="plain-list">
                {preview.decision_requests.map((item) => (
                  <li key={item.code}>
                    <strong>{item.code}</strong>: {item.question}
                  </li>
                ))}
              </ul>
            </article>
          ) : null}

          <article className="card">
            <h2>PM assumptions</h2>
            <ul className="plain-list">
              {preview.assumptions.map((item) => (
                <li key={item.code}>{item.statement}</li>
              ))}
            </ul>
          </article>

          <article className="card">
            <h2>Expected jobs / dependencies</h2>
            {preview.expected_jobs.length === 0 ? (
              <p className="muted">No jobs in this preview.</p>
            ) : (
              <ul className="plain-list">
                {preview.expected_jobs.map((job) => (
                  <li key={job.human_id}>
                    {queueLabel(job.queue)} · {job.human_id}
                    {job.depends_on.length > 0 ? ` · depends on ${job.depends_on.join(", ")}` : ""}
                  </li>
                ))}
              </ul>
            )}
          </article>
        </div>
      ) : null}
    </section>
  );
}
