import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { ReleaseDetailResponse, ReleaseListResponse } from "../api/types";
import { ProjectNav } from "../components/ProjectNav";

function dtoText(value: string | number | boolean | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "Not reported";
  }
  return String(value);
}

export function ReleaseCenterPage() {
  const { projectHumanId, releaseHumanId } = useParams();
  const [list, setList] = useState<ReleaseListResponse | null>(null);
  const [detail, setDetail] = useState<ReleaseDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectHumanId) {
      return;
    }
    const id = projectHumanId;
    const selected = releaseHumanId;
    let cancelled = false;
    async function load() {
      try {
        if (selected) {
          const body = await api.projectRelease(id, selected);
          if (!cancelled) {
            setDetail(body);
            setError(null);
          }
        } else {
          const body = await api.projectReleases(id);
          if (!cancelled) {
            setList(body);
            setDetail(null);
            setError(null);
          }
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load releases");
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectHumanId, releaseHumanId]);

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
  if (releaseHumanId && !detail) {
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <p className="muted">Loading release…</p>
      </section>
    );
  }
  if (!releaseHumanId && !list) {
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <p className="muted">Loading release center…</p>
      </section>
    );
  }

  const base = `/projects/${encodeURIComponent(projectHumanId)}/releases`;

  if (detail) {
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <p>
          <Link to={base}>All releases</Link>
        </p>
        <h1>{detail.release_human_id}</h1>
        <p className="muted">
          Gate {detail.gate} · {detail.status} · QA {detail.qa_recommendation}
        </p>
        <div className="overview-grid">
          <article className="card">
            <h2>Identity</h2>
            <p className="muted">integrated {dtoText(detail.integrated_sha)}</p>
            <p className="muted">released {dtoText(detail.released_sha)}</p>
            <p className="muted">iteration {dtoText(detail.iteration_human_id)}</p>
          </article>
          <article className="card">
            <h2>QA recommendation</h2>
            <p className="stat">{detail.qa_recommendation_detail.status}</p>
            {detail.qa_recommendation_detail.reasons.length === 0 ? (
              <p className="muted">No blocking reasons</p>
            ) : (
              <ul className="plain-list">
                {detail.qa_recommendation_detail.reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            )}
          </article>
          <article className="card span-2">
            <h2>Known findings</h2>
            {detail.known_findings.length === 0 ? (
              <p className="muted">No findings reported</p>
            ) : (
              <ul className="plain-list">
                {detail.known_findings.map((item, index) => (
                  <li key={`${item.kind}-${index}`}>
                    {item.kind} · {dtoText(item.result)} · {dtoText(item.role)}
                  </li>
                ))}
              </ul>
            )}
          </article>
          <article className="card">
            <h2>Release notes</h2>
            <pre className="notes">{dtoText(detail.release_notes)}</pre>
          </article>
          <article className="card">
            <h2>Migration / rollback</h2>
            <p className="muted">migration {dtoText(detail.migration_notes)}</p>
            <pre className="notes">{dtoText(detail.rollback_notes)}</pre>
          </article>
          <article className="card span-2">
            <h2>Manifest / checksums</h2>
            {detail.checksums.length === 0 ? (
              <p className="muted">No cataloged artifacts</p>
            ) : (
              <ul className="plain-list">
                {detail.checksums.map((item) => (
                  <li key={item.filename}>
                    {item.filename} · {item.sha256}
                  </li>
                ))}
              </ul>
            )}
          </article>
          <article className="card span-2">
            <h2>Artifacts</h2>
            <p className="muted">Downloads are keyed by artifact ID, not filesystem paths.</p>
            {detail.artifacts.length === 0 ? (
              <p className="muted">No artifacts cataloged</p>
            ) : (
              <ul className="plain-list">
                {detail.artifacts.map((item) => (
                  <li key={item.artifact_human_id}>
                    <a
                      href={api.releaseArtifactUrl(
                        projectHumanId,
                        detail.release_human_id,
                        item.artifact_human_id,
                      )}
                    >
                      {item.filename}
                    </a>{" "}
                    · {item.kind} · {item.byte_size} bytes
                  </li>
                ))}
              </ul>
            )}
          </article>
        </div>
      </section>
    );
  }

  return (
    <section className="page">
      <ProjectNav projectHumanId={projectHumanId} />
      <h1>Release center</h1>
      <p className="muted">
        Gate status, candidate identity, and allowlisted artifact download. Worker success is not
        release approval.
      </p>
      {list && list.releases.length === 0 ? (
        <p className="muted">No RELEASE jobs for this project</p>
      ) : (
        <ul className="plain-list">
          {list?.releases.map((item) => (
            <li key={item.release_human_id}>
              <Link to={`${base}/${encodeURIComponent(item.release_human_id)}`}>
                {item.release_human_id}
              </Link>{" "}
              · gate {item.gate} · {item.qa_recommendation} · SHA {dtoText(item.integrated_sha)}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
