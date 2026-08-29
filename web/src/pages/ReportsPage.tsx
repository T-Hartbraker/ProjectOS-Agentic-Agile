import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type {
  ReportDashboardResponse,
  ReportResponse,
  ReportSnapshotSummary,
} from "../api/types";
import { ProjectNav } from "../components/ProjectNav";
import { TechnicalDetails } from "../components/TechnicalDetails";
import { reportNarrative } from "../presentation/reportNarrative";

function dtoText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "Not reported";
  }
  return String(value);
}

function BodyView({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === "") {
    return <span className="muted">Not reported</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <p className="muted">None reported</p>;
    }
    return (
      <ul className="plain-list">
        {value.map((item, index) => (
          <li key={index}>
            {typeof item === "object" ? <BodyView value={item} /> : dtoText(item)}
          </li>
        ))}
      </ul>
    );
  }
  if (typeof value === "object") {
    return (
      <ul className="plain-list">
        {Object.entries(value as Record<string, unknown>).map(([key, item]) => (
          <li key={key}>
            <strong>{key}</strong>:{" "}
            {item !== null && typeof item === "object" ? <BodyView value={item} /> : dtoText(item)}
          </li>
        ))}
      </ul>
    );
  }
  return <>{dtoText(value)}</>;
}

function DownloadLinks({
  projectHumanId,
  kind,
  snapshotHumanId,
}: {
  projectHumanId: string;
  kind?: string;
  snapshotHumanId?: string;
}) {
  const html = snapshotHumanId
    ? api.projectReportSnapshotDownloadUrl(projectHumanId, snapshotHumanId, "html")
    : api.projectReportDownloadUrl(projectHumanId, kind ?? "", "html");
  const markdown = snapshotHumanId
    ? api.projectReportSnapshotDownloadUrl(projectHumanId, snapshotHumanId, "markdown")
    : api.projectReportDownloadUrl(projectHumanId, kind ?? "", "markdown");
  const pdf = snapshotHumanId
    ? api.projectReportSnapshotDownloadUrl(projectHumanId, snapshotHumanId, "pdf")
    : api.projectReportDownloadUrl(projectHumanId, kind ?? "", "pdf");
  return (
    <p className="muted">
      Snapshot files (not system of record):{" "}
      <a href={html}>HTML</a>
      {" · "}
      <a href={markdown}>Markdown</a>
      {" · "}
      <a href={pdf}>PDF</a>
    </p>
  );
}

export function ReportsPage() {
  const { projectHumanId, kind, snapshotHumanId } = useParams();
  const navigate = useNavigate();
  const [board, setBoard] = useState<ReportDashboardResponse | null>(null);
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!projectHumanId) {
      return;
    }
    const id = projectHumanId;
    let cancelled = false;
    async function load() {
      try {
        if (snapshotHumanId) {
          const body = await api.projectReportSnapshot(id, snapshotHumanId);
          if (!cancelled) {
            setReport(body);
            setBoard(null);
            setError(null);
          }
        } else if (kind) {
          const body = await api.projectReport(id, kind);
          if (!cancelled) {
            setReport(body);
            setBoard(null);
            setError(null);
          }
        } else {
          const body = await api.projectReportDashboard(id);
          if (!cancelled) {
            setBoard(body);
            setReport(null);
            setError(null);
          }
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load reports");
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectHumanId, kind, snapshotHumanId]);

  async function saveSnapshot() {
    if (!projectHumanId || !report || report.origin === "snapshot") {
      return;
    }
    setSaving(true);
    try {
      const saved = await api.saveProjectReportSnapshot(projectHumanId, report.report_kind);
      navigate(
        `/projects/${encodeURIComponent(projectHumanId)}/reports/snapshots/${encodeURIComponent(saved.snapshot_human_id)}`,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save snapshot");
    } finally {
      setSaving(false);
    }
  }

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
  if ((kind || snapshotHumanId) && !report) {
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <p className="muted">Loading report…</p>
      </section>
    );
  }
  if (!kind && !snapshotHumanId && !board) {
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <p className="muted">Loading reports dashboard…</p>
      </section>
    );
  }

  const base = `/projects/${encodeURIComponent(projectHumanId)}/reports`;

  if (report) {
    const isSnapshot = report.origin === "snapshot";
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <p>
          <Link to={base}>Reports dashboard</Link>
        </p>
        <p className={isSnapshot ? "badge snapshot" : "badge live"}>
          {isSnapshot ? "Historical snapshot" : "Live collected status"}
        </p>
        <h1>{report.title}</h1>
        <p className="muted">
          revision {report.revision} · generated {report.generated_at} · iteration{" "}
          {dtoText(report.iteration_human_id)} · release {dtoText(report.release_human_id)}
          {report.saved_at ? ` · saved ${report.saved_at}` : ""}
        </p>
        <DownloadLinks
          projectHumanId={projectHumanId}
          kind={report.report_kind}
          snapshotHumanId={report.snapshot_human_id ?? undefined}
        />
        {isSnapshot ? (
          <p className="muted">This document is frozen. It is not live project state.</p>
        ) : (
          <p>
            <button type="button" onClick={() => void saveSnapshot()} disabled={saving}>
              {saving ? "Saving snapshot…" : "Save historical snapshot"}
            </button>
          </p>
        )}
        <div className="narrative-grid">
          {reportNarrative([report]).map((section) => (
            <article className="card" key={section.id}>
              <h2>{section.title}</h2>
              <p>{section.body}</p>
            </article>
          ))}
        </div>
        <TechnicalDetails title="Evidence & provenance">
          <p>revision {report.revision}</p>
          <p>generated {report.generated_at}</p>
          {report.sources.length === 0 ? (
            <p>No sources cited</p>
          ) : (
            <ul>
              {report.sources.map((source) => (
                <li key={`${source.entity_type}-${source.entity_human_id}`}>
                  {source.entity_type} {source.entity_human_id} @ {dtoText(source.timestamp)}
                </li>
              ))}
            </ul>
          )}
          <BodyView value={report.body} />
        </TechnicalDetails>
      </section>
    );
  }

  return (
    <section className="page">
      <ProjectNav projectHumanId={projectHumanId} />
      <h1>Reports</h1>
      <p className="badge live">Live collected status</p>
      <p className="muted">{board?.notice}</p>
      <div className="narrative-grid">
        {reportNarrative(board?.reports ?? []).map((section) => (
          <article className="card" key={section.id}>
            <h2>{section.title}</h2>
            <p>{section.body}</p>
          </article>
        ))}
      </div>
      <TechnicalDetails title="Evidence & provenance">
        <p>
          Board generated {dtoText(board?.generated_at)} · iteration{" "}
          {dtoText(board?.iteration_human_id)}
        </p>
        <ul>
          {board?.reports.map((item) => (
            <li key={item.report_kind}>
              <Link to={`${base}/${encodeURIComponent(item.report_kind)}`}>{item.title}</Link>
              {" · "}
              {item.report_kind} · {item.revision}
            </li>
          ))}
        </ul>
        <DownloadLinks projectHumanId={projectHumanId} kind={board?.reports[0]?.report_kind} />
      </TechnicalDetails>
      <h2>Historical snapshots</h2>
      <p className="badge snapshot">Saved documents — not live status</p>
      {board && board.snapshots.length === 0 ? (
        <p className="muted">No snapshots saved yet. Open a live report and save one.</p>
      ) : (
        <ul className="plain-list">
          {board?.snapshots.map((item: ReportSnapshotSummary) => (
            <li key={item.snapshot_human_id}>
              <Link
                to={`${base}/snapshots/${encodeURIComponent(item.snapshot_human_id)}`}
              >
                {item.report_kind} · {item.revision}
              </Link>{" "}
              · generated {item.generated_at} · saved {item.saved_at}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
