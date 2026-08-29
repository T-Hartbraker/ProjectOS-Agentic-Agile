import type { ReportResponse } from "../api/types";
import { healthLabel, queueLabel, statusLabel } from "./labels";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function count(value: unknown): number {
  if (typeof value === "number") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.length;
  }
  return 0;
}

export type NarrativeSection = {
  id: string;
  title: string;
  body: string;
};

export function reportNarrative(reports: ReportResponse[]): NarrativeSection[] {
  const byKind = new Map(reports.map((item) => [item.report_kind, item]));
  const status = byKind.get("project-status");
  const iteration = byKind.get("iteration-review");
  const quality = byKind.get("quality");
  const release = byKind.get("release");
  const risks = byKind.get("risks");
  const sections: NarrativeSection[] = [];

  if (status) {
    const body = asRecord(status.body);
    sections.push({
      id: "status",
      title: "Project status",
      body: `This project is ${healthLabel(String(body.health ?? "")).toLowerCase()}.`,
    });
  }
  if (iteration) {
    const body = asRecord(iteration.body);
    const open = count(body.open_jobs);
    sections.push({
      id: "iteration",
      title: "Current iteration",
      body: body.iteration_human_id
        ? `Work is on ${String(body.iteration_human_id)} with ${open} open jobs.`
        : "No current iteration is reported.",
    });
    sections.push({
      id: "progress",
      title: "Progress",
      body:
        open === 0
          ? "No open jobs remain in the current iteration."
          : `${open} jobs still need to finish before this iteration is complete.`,
    });
  }
  if (quality) {
    const summary = asRecord(asRecord(quality.body).summary);
    const passed = Number(summary.passed_count ?? 0);
    const failed = Number(summary.failed_count ?? 0);
    const pending = Number(summary.pending_count ?? 0);
    sections.push({
      id: "quality",
      title: "Quality",
      body:
        failed === 0 && pending === 0
          ? `All ${passed} independent assurance checks passed. No defects are currently blocking.`
          : `${passed} checks passed, ${failed} failed, ${pending} still pending.`,
    });
    const security = String(asRecord(asRecord(quality.body).role_results).ASSURANCE_SECURITY ?? "");
    sections.push({
      id: "security",
      title: "Security",
      body: security
        ? `Security review is ${statusLabel(security).toLowerCase()}.`
        : "Security review has not reported a result.",
    });
  }
  if (release) {
    const latest = asRecord(asRecord(release.body).latest);
    sections.push({
      id: "release",
      title: "Release readiness",
      body: latest.gate
        ? `Release gate is ${statusLabel(String(latest.gate)).toLowerCase()}.`
        : "No release gate is reported yet.",
    });
  }
  if (risks) {
    const issues = count(asRecord(risks.body).issue_count);
    sections.push({
      id: "issues",
      title: "Open issues",
      body: issues === 0 ? "No open issues are reported." : `${issues} issues need operator attention.`,
    });
  }
  sections.push({
    id: "next",
    title: "Next actions",
    body: nextAction(reports),
  });
  return sections;
}

function nextAction(reports: ReportResponse[]): string {
  const quality = reports.find((item) => item.report_kind === "quality");
  if (quality) {
    const summary = asRecord(asRecord(quality.body).summary);
    if (Number(summary.failed_count ?? 0) > 0) {
      return "Resolve failed assurance before asking for release.";
    }
    if (Number(summary.pending_count ?? 0) > 0) {
      return "Wait for remaining independent reviews to finish.";
    }
  }
  const release = reports.find((item) => item.report_kind === "release");
  if (release) {
    const latest = asRecord(asRecord(release.body).latest);
    if (String(latest.gate ?? "") === "ready") {
      return "Ask a Sponsor to approve release if policy requires it.";
    }
  }
  return "Submit new work if the current iteration is complete.";
}

export function recentActivityLine(kind: string | null, status: string | null, _jobId: string): string {
  return `${queueLabel(kind)} ${statusLabel(status).toLowerCase()}.`;
}
