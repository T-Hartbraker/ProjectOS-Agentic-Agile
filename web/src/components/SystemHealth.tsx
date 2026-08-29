import type { DaemonStatusResponse, HealthComponent, HealthResponse } from "../api/types";

type Props = {
  health: HealthResponse | null;
  daemon: DaemonStatusResponse | null;
  apiError: string | null;
};

function toneFor(status: string | null): string {
  if (status === "ok" || status === "connected") {
    return "ok";
  }
  if (
    status === "disabled" ||
    status === "stopped" ||
    status === "degraded" ||
    status === "not_configured" ||
    status === "connecting" ||
    status === "disconnected"
  ) {
    return "warn";
  }
  if (status === "error" || status === "down") {
    return "bad";
  }
  return "warn";
}

function displayStatus(name: string, status: string): string {
  if (name === "daemon" && status === "ok") {
    return "running";
  }
  return status;
}

function operatorOverall(components: HealthComponent[], apiError: string | null): string {
  if (apiError) {
    return "down";
  }
  const failed = components.filter(
    (item) => item.required && !["ok", "disabled", "connected"].includes(item.status),
  );
  if (failed.some((item) => item.status === "error")) {
    return "degraded";
  }
  if (failed.length > 0) {
    return "degraded";
  }
  return "ok";
}

function mergeDaemonComponent(
  components: HealthComponent[],
  daemon: DaemonStatusResponse | null,
): HealthComponent[] {
  if (!daemon || daemon.status !== "running") {
    return components;
  }
  return components.map((item) => {
    if (item.name !== "daemon") {
      return item;
    }
    if (item.status === "ok" || item.status === "running") {
      return item;
    }
    const pid = daemon.pid;
    const detail = pid ? `running pid ${pid}` : item.detail || "running";
    return {
      ...item,
      status: "ok",
      detail,
      pid: pid ?? item.pid,
    };
  });
}

function fallbackComponents(
  health: HealthResponse | null,
  daemon: DaemonStatusResponse | null,
  apiError: string | null,
): HealthComponent[] {
  if (health?.components && health.components.length > 0) {
    return mergeDaemonComponent(health.components, daemon);
  }
  const apiStatus = health ? "ok" : apiError ? "down" : "stopped";
  const daemonStatus = daemon?.status ?? "unknown";
  return [
    {
      name: "api",
      status: apiStatus,
      required: true,
      detail: health?.status ?? apiError ?? "unknown",
      pid: null,
    },
    {
      name: "daemon",
      status: daemonStatus === "running" ? "ok" : daemonStatus,
      required: true,
      detail: daemon?.last_error || daemonStatus,
      pid: daemon?.pid ?? null,
    },
  ];
}

export function SystemHealth({ health, daemon, apiError }: Props) {
  const components = fallbackComponents(health, daemon, apiError);
  const overall = operatorOverall(components, apiError);
  const overallTone = apiError ? "bad" : toneFor(overall);
  return (
    <div className="health-cluster" aria-label="System health">
      <div className={`pill ${overallTone}`}>
        <span className="label">Operator</span>
        <strong>{overall}</strong>
      </div>
      {components.map((item) => (
        <div key={item.name} className={`pill ${toneFor(item.status)}`} title={item.detail}>
          <span className="label">{item.name.replace("_", " ")}</span>
          <strong>{displayStatus(item.name, item.status)}</strong>
        </div>
      ))}
    </div>
  );
}
