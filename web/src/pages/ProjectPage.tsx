import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { ProjectionResponse, SchedulerEntryResponse } from "../api/types";
import { ProjectNav } from "../components/ProjectNav";
import { ProjectOverview } from "../components/ProjectOverview";

export function ProjectPage() {
  const { projectHumanId } = useParams();
  const [projection, setProjection] = useState<ProjectionResponse | null>(null);
  const [schedule, setSchedule] = useState<SchedulerEntryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectHumanId) {
      return;
    }
    const id = projectHumanId;
    let cancelled = false;
    let timer: number | undefined;
    setProjection(null);
    setSchedule(null);
    setError(null);

    async function load() {
      try {
        const [snapshot, scheduler] = await Promise.all([
          api.projectProjection(id),
          api.scheduler().catch(() => null),
        ]);
        if (cancelled) {
          return;
        }
        setProjection(snapshot);
        setSchedule(
          scheduler?.schedules.find((entry) => entry.project_human_id === id) ?? null,
        );
        setError(null);
        const waitMs = Math.max(5, snapshot.poll_after_seconds) * 1000;
        timer = window.setTimeout(() => {
          void load();
        }, waitMs);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load project");
        }
      }
    }

    void load();
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [projectHumanId]);

  if (!projectHumanId) {
    return null;
  }
  if (error) {
    return (
      <section className="page">
        <div className="banner error">{error}</div>
      </section>
    );
  }
  if (!projection) {
    return (
      <section className="page">
        <p className="muted">Loading {projectHumanId}…</p>
      </section>
    );
  }
  return (
    <>
      <ProjectNav projectHumanId={projectHumanId} />
      <ProjectOverview projection={projection} schedule={schedule} />
    </>
  );
}
