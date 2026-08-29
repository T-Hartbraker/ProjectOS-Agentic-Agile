import type { ProjectionResponse, SchedulerEntryResponse } from "../api/types";
import { TechnicalDetails } from "./TechnicalDetails";
import { healthLabel, queueLabel, statusLabel, statusTone } from "../presentation/labels";

type Props = {
  projection: ProjectionResponse;
  schedule: SchedulerEntryResponse | null;
};

function runningItems(projection: ProjectionResponse) {
  return projection.jobs.items.filter((job) => job.status === "RUNNING" || job.status === "LEASED");
}

function blockedItems(projection: ProjectionResponse) {
  return [
    ...projection.jobs.items.filter((job) => job.status === "BLOCKED" || job.status === "FAILED"),
    ...projection.errors,
  ];
}

export function ProjectOverview({ projection, schedule }: Props) {
  const running = runningItems(projection);
  const blocked = blockedItems(projection);
  const finished = projection.jobs.items.filter((job) => job.status === "SUCCEEDED").length;
  const qualityPassed = projection.assurance.passed_count;
  const qualityRequired = projection.assurance.required_roles.length;

  return (
    <section className="page overview">
      <header className="overview-header">
        <h1>{projection.project_human_id}</h1>
        <p className="muted">{projection.headline}</p>
      </header>

      <article className="now-card">
        <h2>What is happening</h2>
        {running.length === 0 ? (
          <>
            <h3>No work in progress</h3>
            <p className="muted">Nothing is currently running. Check blocked work or submit new work.</p>
          </>
        ) : (
          running.map((job) => (
            <div key={job.human_id}>
              <h3>{queueLabel(job.queue)}</h3>
              <p>{queueLabel(job.queue)} is in progress{job.work_item_human_id ? ` for ${job.work_item_human_id}` : ""}.</p>
              <p className={`status-chip ${statusTone(job.status)}`}>{statusLabel(job.status)}</p>
            </div>
          ))
        )}
      </article>

      <div className="narrative-grid">
        <article className="card">
          <h2>Is the project healthy?</h2>
          <p className={`stat health-${projection.health.status}`}>{healthLabel(projection.health.status)}</p>
          {projection.health.reasons[0] ? <p>{projection.health.reasons[0]}</p> : <p className="muted">No health issues reported.</p>}
        </article>
        <article className="card">
          <h2>What finished</h2>
          <p className="stat">{finished}</p>
          <p className="muted">Finished jobs in the current projection.</p>
        </article>
        <article className="card">
          <h2>What is blocked</h2>
          <p className="stat">{blocked.length}</p>
          {blocked.length === 0 ? (
            <p className="muted">Nothing is blocked.</p>
          ) : (
            <ul className="plain-list">
              {projection.jobs.items
                .filter((job) => job.status === "BLOCKED" || job.status === "FAILED")
                .slice(0, 5)
                .map((job) => (
                  <li key={job.human_id}>
                    {queueLabel(job.queue)} is {statusLabel(job.status).toLowerCase()}
                    {job.last_error ? `: ${job.last_error}` : ""}
                  </li>
                ))}
            </ul>
          )}
        </article>
        <article className="card">
          <h2>What happens next</h2>
          <p>
            {running.length > 0
              ? "Independent reviews or release verification start when the current job succeeds."
              : blocked.length > 0
                ? "Resolve blocked work before starting new delivery."
                : "Submit new work if the iteration is complete."}
          </p>
        </article>
        <article className="card">
          <h2>Quality</h2>
          <p>
            {qualityRequired === 0
              ? "No independent reviews are required yet."
              : `${qualityPassed} of ${qualityRequired} independent reviews have passed.`}
          </p>
        </article>
        <article className="card">
          <h2>Current iteration</h2>
          <p className="stat">{projection.approvals.iteration_human_id ?? "None"}</p>
          <p className="muted">
            {projection.approvals.has_accepted_plan ? "An accepted plan is in place." : "No accepted plan yet."}
          </p>
        </article>
      </div>

      <TechnicalDetails title="Evidence & provenance">
        <p>revision {projection.revision}</p>
        <p>generated {projection.generated_at}</p>
        <p>paused={String(projection.health.paused)}</p>
        {schedule ? (
          <p>
            schedule {schedule.cadence} {schedule.local_time} window {schedule.window_key}
          </p>
        ) : (
          <p>No scheduler record</p>
        )}
        <ul>
          {projection.jobs.items.slice(0, 12).map((job) => (
            <li key={job.human_id}>
              {job.human_id} · {job.queue} · {job.status}
            </li>
          ))}
        </ul>
      </TechnicalDetails>
    </section>
  );
}
