export const STATUS_LABELS: Record<string, string> = {
  QUEUED: "Queued",
  READY: "Ready",
  LEASED: "Assigned",
  RUNNING: "In progress",
  SUCCEEDED: "Finished",
  FAILED: "Failed",
  BLOCKED: "Blocked",
  RETRY_WAIT: "Waiting to retry",
  CANCELLED: "Cancelled",
};

export const QUEUE_LABELS: Record<string, string> = {
  DELIVERY: "Delivery",
  INTEGRATION: "Integration",
  RELEASE: "Release",
  PM: "Planning",
  ASSURANCE_FUNCTIONAL: "Functional review",
  ASSURANCE_QUALITY: "Quality review",
  ASSURANCE_SECURITY: "Security review",
  ASSURANCE_INTEGRATION: "Integration review",
};

export const ROLE_LABELS: Record<string, string> = {
  DELIVERY: "Delivery agent",
  INTEGRATION: "Integration agent",
  RELEASE: "Release agent",
  PM: "Planner",
  ASSURANCE_FUNCTIONAL: "Functional reviewer",
  ASSURANCE_QUALITY: "Quality reviewer",
  ASSURANCE_SECURITY: "Security reviewer",
  ASSURANCE_INTEGRATION: "Integration reviewer",
};

export const LANE_LABELS: Record<string, string> = {
  delivery: "Delivery",
  assurance: "Quality",
  control: "Control",
};

export const HEALTH_LABELS: Record<string, string> = {
  healthy: "Healthy",
  degraded: "Needs attention",
  paused: "Paused",
  blocked: "Blocked",
  disabled: "Disabled",
};

export function humanizeToken(value: string | null | undefined): string {
  const text = String(value ?? "").trim();
  if (!text) {
    return "Not reported";
  }
  return text
    .replace(/[_-]+/g, " ")
    .split(/\s+/)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");
}

export function statusLabel(status: string | null | undefined): string {
  const key = String(status ?? "").trim();
  return STATUS_LABELS[key] ?? humanizeToken(key);
}

export function queueLabel(queue: string | null | undefined): string {
  const key = String(queue ?? "").trim();
  return QUEUE_LABELS[key] ?? humanizeToken(key);
}

export function roleLabel(role: string | null | undefined): string {
  const key = String(role ?? "").trim();
  return ROLE_LABELS[key] ?? humanizeToken(key);
}

export function laneLabel(lane: string | null | undefined): string {
  const key = String(lane ?? "").trim();
  return LANE_LABELS[key] ?? humanizeToken(key);
}

export function healthLabel(status: string | null | undefined): string {
  const key = String(status ?? "").trim().toLowerCase();
  return HEALTH_LABELS[key] ?? humanizeToken(key);
}

export function statusTone(status: string | null | undefined): "ok" | "warn" | "bad" | "idle" {
  const key = String(status ?? "").toUpperCase();
  if (["SUCCEEDED", "HEALTHY", "OK", "PASS", "PASSED"].includes(key)) {
    return "ok";
  }
  if (["RUNNING", "READY", "LEASED", "QUEUED", "RETRY_WAIT", "PAUSED", "DEGRADED"].includes(key)) {
    return "warn";
  }
  if (["FAILED", "BLOCKED", "CANCELLED", "ERROR", "DOWN"].includes(key)) {
    return "bad";
  }
  return "idle";
}

export function shortSha(value: string | null | undefined): string {
  if (!value) {
    return "Not reported";
  }
  return value.length > 12 ? `${value.slice(0, 12)}…` : value;
}

export function elapsedSince(startedAt: string | null | undefined, now = Date.now()): string {
  if (!startedAt) {
    return "Start time not reported";
  }
  const start = Date.parse(startedAt);
  if (Number.isNaN(start)) {
    return startedAt;
  }
  const minutes = Math.max(0, Math.round((now - start) / 60000));
  if (minutes < 1) {
    return "Started just now";
  }
  if (minutes === 1) {
    return "Started 1 minute ago";
  }
  if (minutes < 60) {
    return `Started ${minutes} minutes ago`;
  }
  const hours = Math.round(minutes / 60);
  return hours === 1 ? "Started 1 hour ago" : `Started ${hours} hours ago`;
}
