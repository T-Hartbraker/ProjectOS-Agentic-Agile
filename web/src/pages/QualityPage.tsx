import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { QualityFinding, QualityResponse } from "../api/types";
import { ProjectNav } from "../components/ProjectNav";
import { TechnicalDetails } from "../components/TechnicalDetails";
import { roleLabel, statusLabel } from "../presentation/labels";

function dtoText(value: string | number | boolean | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "Not reported";
  }
  return String(value);
}

function countEntries(counts: Record<string, number>) {
  const entries = Object.entries(counts);
  if (entries.length === 0) {
    return <p className="muted">None reported</p>;
  }
  return (
    <ul className="plain-list">
      {entries.map(([key, count]) => (
        <li key={key}>
          {key}: {count}
        </li>
      ))}
    </ul>
  );
}

function FindingCard({ title, finding }: { title: string; finding: QualityFinding | null }) {
  if (!finding) {
    return (
      <article className="card">
        <h2>{title}</h2>
        <p className="muted">Not reported</p>
      </article>
    );
  }
  return (
    <article className="card">
      <h2>{title}</h2>
      <p className="stat">{statusLabel(finding.result)}</p>
      <TechnicalDetails>
        <p>candidate {dtoText(finding.candidate_git_sha)}</p>
        <p>evidence {dtoText(finding.evidence_ref)}</p>
        <p>job {dtoText(finding.job_human_id)}</p>
      </TechnicalDetails>
    </article>
  );
}

export function QualityPage() {
  const { projectHumanId } = useParams();
  const [quality, setQuality] = useState<QualityResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectHumanId) {
      return;
    }
    const id = projectHumanId;
    let cancelled = false;
    async function load() {
      try {
        const body = await api.projectQuality(id);
        if (!cancelled) {
          setQuality(body);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load quality");
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectHumanId]);

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
  if (!quality) {
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <p className="muted">Loading quality view…</p>
      </section>
    );
  }

  const { summary, evidence, findings, defects, defect_counts, lineage, release_blocking_reasons } =
    quality;

  return (
    <section className="page">
      <ProjectNav projectHumanId={projectHumanId} />
      <h1>Quality and defects</h1>
      <p className="muted">
        Independent assurance only. Developer runs cannot mark QA passed (
        {quality.qa_pass_authority}).
      </p>
      <p className="muted">
        Dashboard write: {quality.developer_can_mark_qa_passed ? "allowed" : "disabled"}
      </p>

      <div className="overview-grid">
        <article className="card">
          <h2>QA summary</h2>
          <p className="stat">
            pass {summary.passed_count} / fail {summary.failed_count}
          </p>
          <p className="muted">
            pending {summary.pending_count} · stale {summary.stale_count} · open{" "}
            {summary.open_assurance_jobs}
          </p>
          <ul className="plain-list">
            {summary.required_roles.map((role) => (
              <li key={role}>
                {roleLabel(role)}: {statusLabel(summary.role_results[role] ?? "Not reported")}
              </li>
            ))}
          </ul>
          <p className="muted">
            candidate SHA evaluated:{" "}
            {summary.evaluated_candidate_shas.length > 0
              ? summary.evaluated_candidate_shas.join(", ")
              : "Not reported"}
          </p>
        </article>

        <FindingCard title="Security findings" finding={findings.security} />
        <FindingCard title="Code-quality findings" finding={findings.quality} />

        <article className="card">
          <h2>Defects</h2>
          <p className="stat">{defects.length}</p>
          <h3>By severity</h3>
          {countEntries(defect_counts.by_severity)}
          <h3>By priority</h3>
          {countEntries(defect_counts.by_priority)}
          <h3>By status</h3>
          {countEntries(defect_counts.by_status)}
          {defects.length === 0 ? (
            <p className="muted">No defects in quality DTO</p>
          ) : (
            <ul className="plain-list">
              {defects.map((defect) => (
                <li key={defect.defect_human_id}>
                  {defect.defect_human_id} · {defect.status} · severity {defect.severity} ·
                  priority {defect.priority} · {dtoText(defect.assurance_role)} · SHA{" "}
                  {dtoText(defect.candidate_git_sha)}
                </li>
              ))}
            </ul>
          )}
        </article>

        <article className="card span-2">
          <h2>Test evidence</h2>
          {evidence.length === 0 ? (
            <p className="muted">No QA evidence</p>
          ) : (
            <ul className="plain-list">
              {evidence.map((item, index) => (
                <li key={`${item.assurance_job_human_id}-${index}`}>
                  {item.assurance_role} · {item.result} · SHA {dtoText(item.candidate_git_sha)} ·
                  evidence {dtoText(item.evidence_ref)} · job{" "}
                  {dtoText(item.assurance_job_human_id)}
                </li>
              ))}
            </ul>
          )}
        </article>

        <article className="card span-2">
          <h2>Rework / retest lineage</h2>
          {lineage.length === 0 ? (
            <p className="muted">No rework or invalidation lineage</p>
          ) : (
            <ul className="plain-list">
              {lineage.map((item, index) => (
                <li key={`${item.kind}-${item.rework_job_human_id}-${index}`}>
                  {item.kind} · {dtoText(item.reason)} · rework {dtoText(item.rework_job_human_id)} ·
                  retest {dtoText(item.retest_job_human_id)} · SHA{" "}
                  {dtoText(item.candidate_git_sha ?? item.invalidated_candidate_sha)}
                </li>
              ))}
            </ul>
          )}
        </article>

        <article className="card span-2">
          <h2>Release-blocking reasons</h2>
          {release_blocking_reasons.length === 0 ? (
            <p className="muted">No blocking reasons reported</p>
          ) : (
            <ul className="plain-list">
              {release_blocking_reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
        </article>
      </div>
    </section>
  );
}
