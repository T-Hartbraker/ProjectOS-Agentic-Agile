import { FormEvent, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { AuditResponse } from "../api/types";
import { ProjectNav } from "../components/ProjectNav";

function dtoText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "Not reported";
  }
  return String(value);
}

export function AuditPage() {
  const { projectHumanId } = useParams();
  const [payload, setPayload] = useState<AuditResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actorType, setActorType] = useState("");
  const [source, setSource] = useState("");
  const [action, setAction] = useState("");

  async function reload(id: string) {
    const body = await api.projectAudit(id, {
      actor_type: actorType || undefined,
      source: source || undefined,
      action: action || undefined,
    });
    setPayload(body);
    setError(null);
  }

  useEffect(() => {
    if (!projectHumanId) {
      return;
    }
    const id = projectHumanId;
    let cancelled = false;
    async function load() {
      try {
        await reload(id);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load audit events");
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectHumanId]);

  async function onFilter(event: FormEvent) {
    event.preventDefault();
    if (!projectHumanId) {
      return;
    }
    try {
      await reload(projectHumanId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to filter audit events");
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
  if (!payload) {
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <p className="muted">Loading audit events…</p>
      </section>
    );
  }

  return (
    <section className="page">
      <ProjectNav projectHumanId={projectHumanId} />
      <h1>Activity</h1>
      <p className="badge live">Projection only</p>
      <p className="muted">{payload.notice}</p>
      <form className="intake-form" onSubmit={(event) => void onFilter(event)}>
        <label>
          Actor type
          <input value={actorType} onChange={(event) => setActorType(event.target.value)} />
        </label>
        <label>
          Source
          <input value={source} onChange={(event) => setSource(event.target.value)} />
        </label>
        <label>
          Action
          <input value={action} onChange={(event) => setAction(event.target.value)} />
        </label>
        <div className="intake-actions">
          <button type="submit">Apply filters</button>
        </div>
      </form>
      {payload.events.length === 0 ? (
        <p className="muted">No audit events for this project</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Source</th>
              <th>Actor</th>
              <th>Action</th>
              <th>Entity</th>
            </tr>
          </thead>
          <tbody>
            {payload.events.map((item, index) => (
              <tr key={`${item.occurred_at}-${item.entity_human_id}-${index}`}>
                <td>{item.occurred_at}</td>
                <td>{item.source}</td>
                <td>
                  {item.actor_type}/{dtoText(item.actor_id)}
                </td>
                <td>{item.action}</td>
                <td>
                  {item.entity_kind} {item.entity_human_id}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
