import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { PortfolioResponse } from "../api/types";
import { healthLabel } from "../presentation/labels";

function dtoText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "Not reported";
  }
  return String(value);
}

export function HomePage() {
  const [payload, setPayload] = useState<PortfolioResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const body = await api.portfolio();
        if (!cancelled) {
          setPayload(body);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load portfolio");
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section className="page">
      <h1>Operator home</h1>
      <p className="muted">
        This is the only normal cross-project view. Each card is loaded from that project&apos;s
        own records. The dashboard talks only to the ProjectOS HTTP API.
      </p>
      {error ? <div className="banner error">{error}</div> : null}
      {payload ? <p className="muted">{payload.notice}</p> : null}
      {!payload && !error ? <p className="muted">Loading portfolio…</p> : null}
      <p>
        <Link to="/projects/new">+ New project</Link>
      </p>
      {payload && payload.projects.length === 0 ? (
        <p className="empty">No registered projects. Create one to start work intake.</p>
      ) : null}
      {payload ? (
        <div className="cards">
          {payload.projects.map((item) => (
            <Link
              key={item.project_human_id}
              className="card"
              to={`/projects/${encodeURIComponent(item.project_human_id)}`}
            >
              <h2>{item.project_human_id}</h2>
              <p className="stat">{healthLabel(item.health)}</p>
              <p className="muted">Iteration {dtoText(item.current_iteration_human_id)}</p>
              <p className="muted">
                Active jobs {item.active_job_count} · Blockers {item.blocker_count} · Defects{" "}
                {item.open_defect_count}
              </p>
              <p className="muted">
                Release {dtoText(item.release_human_id)} {dtoText(item.release_status)}
              </p>
              <p className="muted">Learning {item.active_memory_count}</p>
            </Link>
          ))}
        </div>
      ) : null}
    </section>
  );
}
