import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { ActivityResponse, JobGraphResponse, JobResponse } from "../api/types";
import { ProjectNav } from "../components/ProjectNav";
import { TechnicalDetails } from "../components/TechnicalDetails";
import { describeActiveWork, runningJobs } from "../presentation/activeWork";
import { laneLabel, queueLabel, roleLabel, shortSha, statusTone } from "../presentation/labels";

function jobDepth(job: JobResponse, byId: Map<string, JobResponse>): number {
  const seen = new Set<string>();
  function walk(id: string): number {
    if (seen.has(id)) {
      return 0;
    }
    seen.add(id);
    const node = byId.get(id);
    if (!node || node.depends_on.length === 0) {
      return 0;
    }
    return 1 + Math.max(...node.depends_on.map((dep) => walk(dep)));
  }
  return walk(job.human_id);
}

function JobCard({ job }: { job: JobResponse }) {
  const active = describeActiveWork(job);
  return (
    <article className={`job-card lane-${job.lane}`}>
      <header>
        <strong>{active.title}</strong>
        <span className={`status-chip ${statusTone(job.status)}`}>{active.status}</span>
      </header>
      <p>{active.sentence}</p>
      <p className="muted">{active.objective}</p>
      {job.status === "RUNNING" || job.status === "LEASED" ? (
        <>
          <p className="muted">{active.started}</p>
          <p>Next: {active.next}</p>
        </>
      ) : null}
      <TechnicalDetails>
        <p>job {job.human_id}</p>
        <p>queue {job.queue} · role {job.agent_role}</p>
        <p>attempt {job.attempt}/{job.max_attempts}</p>
        <p>candidate {shortSha(job.candidate_git_sha)}</p>
        {job.evidence_ref ? <p>evidence {job.evidence_ref}</p> : null}
        {job.depends_on.length > 0 ? <p>depends on {job.depends_on.join(", ")}</p> : null}
      </TechnicalDetails>
    </article>
  );
}

export function JobsPage() {
  const { projectHumanId } = useParams();
  const [graph, setGraph] = useState<JobGraphResponse | null>(null);
  const [activity, setActivity] = useState<ActivityResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectHumanId) {
      return;
    }
    const id = projectHumanId;
    let cancelled = false;
    async function load() {
      try {
        const [graphBody, activityBody] = await Promise.all([
          api.projectGraph(id),
          api.projectActivity(id),
        ]);
        if (!cancelled) {
          setGraph(graphBody);
          setActivity(activityBody);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load work");
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectHumanId]);

  const ranks = useMemo(() => {
    if (!graph) {
      return [];
    }
    const byId = new Map(graph.nodes.map((job) => [job.human_id, job]));
    const grouped = new Map<number, JobResponse[]>();
    for (const job of graph.nodes) {
      const depth = jobDepth(job, byId);
      grouped.set(depth, [...(grouped.get(depth) ?? []), job]);
    }
    return [...grouped.entries()].sort((a, b) => a[0] - b[0]);
  }, [graph]);

  if (!projectHumanId) {
    return null;
  }
  if (error) {
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <div className="banner error">{error}</div>
      </section>
    );
  }
  if (!graph || !activity) {
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <p className="muted">Loading work…</p>
      </section>
    );
  }

  const active = runningJobs(graph.nodes);
  const delivery = graph.nodes.filter((job) => job.lane === "delivery");
  const assurance = graph.nodes.filter((job) => job.lane === "assurance");
  const control = graph.nodes.filter((job) => job.lane === "control");

  return (
    <section className="page">
      <ProjectNav projectHumanId={projectHumanId} />
      <h1>Work</h1>
      {active.length === 0 ? (
        <article className="now-card">
          <h2>Currently working</h2>
          <h3>Nothing in progress</h3>
          <p className="muted">There is no running work. Check blocked items or submit new work.</p>
        </article>
      ) : (
        active.map((job) => {
          const described = describeActiveWork(job);
          return (
            <article className="now-card" key={job.human_id}>
              <h2>Currently working</h2>
              <h3>{described.title}</h3>
              <p>{described.sentence}</p>
              <p className="muted">{described.objective}</p>
              <p className="muted">{described.started}</p>
              <p>Next: {described.next}</p>
              <p className={`status-chip ${statusTone(job.status)}`}>{described.status}</p>
              <TechnicalDetails title="Evidence & provenance">
                <p>{job.human_id}</p>
                <p>role {roleLabel(job.agent_role)}</p>
                <p>candidate {shortSha(job.candidate_git_sha)}</p>
              </TechnicalDetails>
            </article>
          );
        })
      )}

      <div className="lane-grid">
        <section>
          <h2>{laneLabel("delivery")}</h2>
          {delivery.length === 0 ? <p className="muted">No delivery work</p> : delivery.map((job) => <JobCard key={job.human_id} job={job} />)}
        </section>
        <section>
          <h2>{laneLabel("assurance")}</h2>
          {assurance.length === 0 ? <p className="muted">No reviews</p> : assurance.map((job) => <JobCard key={job.human_id} job={job} />)}
        </section>
        <section>
          <h2>{laneLabel("control")}</h2>
          {control.length === 0 ? <p className="muted">No control work</p> : control.map((job) => <JobCard key={job.human_id} job={job} />)}
        </section>
      </div>

      <TechnicalDetails title="Evidence & provenance">
        <p>Recent agent runs</p>
        {activity.recent_runs.length === 0 ? (
          <p>None</p>
        ) : (
          <ul>
            {activity.recent_runs.map((run, index) => (
              <li key={`${run.job_human_id}-${run.created_at}-${index}`}>
                {run.created_at} · {run.job_human_id} · {run.role} · {shortSha(run.candidate_git_sha)}
              </li>
            ))}
          </ul>
        )}
        <div className="graph">
          {ranks.map(([depth, jobs]) => (
            <div className="graph-rank" key={depth}>
              <span className="muted">step {depth + 1}</span>
              {jobs.map((job) => (
                <p key={job.human_id}>
                  {job.human_id} · {queueLabel(job.queue)}
                </p>
              ))}
            </div>
          ))}
        </div>
      </TechnicalDetails>
    </section>
  );
}
