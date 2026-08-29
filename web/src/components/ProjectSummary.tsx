import type { CurrentStateResponse, ProjectSummaryResponse } from "../api/types";

type Props = {
  summary: ProjectSummaryResponse;
  current: CurrentStateResponse | null;
};

export function ProjectSummary({ summary, current }: Props) {
  const counts = Object.entries(summary.job_counts);
  return (
    <section className="page">
      <h1>{summary.project_human_id}</h1>
      <div className="cards">
        <article className="card">
          <h2>Registry</h2>
          <p className="stat">{summary.enabled ? "Enabled" : "Disabled"}</p>
          <p className="muted">{summary.has_accepted_plan ? "Accepted plan on file" : "No accepted plan"}</p>
        </article>
        <article className="card">
          <h2>Iteration</h2>
          <p className="stat">
            {current?.iteration_human_id ?? summary.current_iteration_human_id ?? "—"}
          </p>
          <p className="muted">
            {current?.from_accepted_plan ? "From accepted plan" : "Not bound to an accepted plan"}
          </p>
        </article>
        <article className="card">
          <h2>Release</h2>
          <p className="stat">
            {current?.release_status ?? summary.current_release_status ?? "—"}
          </p>
          <p className="muted">
            {current?.release_job_human_id ??
              summary.current_release_job_human_id ??
              "No release job"}
          </p>
        </article>
        <article className="card">
          <h2>Jobs</h2>
          {counts.length === 0 ? (
            <p className="muted">No jobs yet.</p>
          ) : (
            <div className="job-counts">
              {counts.map(([status, count]) => (
                <span className="chip" key={status}>
                  {status} {count}
                </span>
              ))}
            </div>
          )}
        </article>
      </div>
    </section>
  );
}
