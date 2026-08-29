import { useEffect, useState } from "react";
import { Outlet, useLocation, useNavigate, useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { DaemonStatusResponse, HealthResponse, ProjectResponse } from "../api/types";
import { DisplayScale } from "./DisplayScale";
import { ProjectSelector } from "./ProjectSelector";
import { SystemHealth } from "./SystemHealth";

export type ShellOutletContext = {
  projects: ProjectResponse[];
  projectHumanId: string | undefined;
};

export function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const { projectHumanId } = useParams();
  const [projects, setProjects] = useState<ProjectResponse[]>([]);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [daemon, setDaemon] = useState<DaemonStatusResponse | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [healthBody, projectList] = await Promise.all([
          api.health(),
          api.listProjects(),
        ]);
        if (cancelled) {
          return;
        }
        setHealth(healthBody);
        setProjects(projectList.projects);
        setApiError(null);
      } catch (err) {
        if (!cancelled) {
          setApiError(err instanceof ApiError ? err.message : "API unreachable");
        }
      }
      try {
        const daemonBody = await api.daemon();
        if (!cancelled) {
          setDaemon(daemonBody);
        }
      } catch {
        if (!cancelled) {
          setDaemon(null);
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [location.pathname]);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <strong>ProjectOS</strong>
          <span>Operator control plane</span>
        </div>
        <div className="topbar-controls">
          <ProjectSelector
            projects={projects}
            selectedId={projectHumanId ?? null}
            onSelect={(id) => {
              if (id) {
                navigate(`/projects/${encodeURIComponent(id)}`);
              } else {
                navigate("/");
              }
            }}
            onNewProject={() => navigate("/projects/new")}
          />
          <DisplayScale />
          <button
            type="button"
            className="settings-link"
            aria-label="Settings"
            title="Settings"
            onClick={() => navigate("/settings")}
          >
            Settings
          </button>
          <SystemHealth health={health} daemon={daemon} apiError={apiError} />
        </div>
      </header>
      <main className="main">
        {apiError ? <div className="banner error">{apiError}</div> : null}
        <Outlet context={{ projects, projectHumanId } satisfies ShellOutletContext} />
      </main>
    </div>
  );
}
