import { FormEvent, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { LearningMemory, LearningResponse } from "../api/types";
import { ProjectNav } from "../components/ProjectNav";

function dtoText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "Not reported";
  }
  return String(value);
}

function MemoryList({
  heading,
  items,
  empty,
}: {
  heading: string;
  items: LearningMemory[];
  empty: string;
}) {
  if (items.length === 0) {
    return (
      <>
        <h2>{heading}</h2>
        <p className="muted">{empty}</p>
      </>
    );
  }
  return (
    <>
      <h2>{heading}</h2>
      <ul className="plain-list">
        {items.map((item) => (
          <li key={item.memory_human_id}>
            {item.memory_human_id} · {item.agent_role} · {item.status}
            {item.rejection_code ? ` · ${item.rejection_code}: ${dtoText(item.rejection_reason)}` : ""}
            {item.superseded_by_memory_human_id
              ? ` · successor ${item.superseded_by_memory_human_id}`
              : ""}
            {` · ${item.title}`}
          </li>
        ))}
      </ul>
    </>
  );
}

export function LearningPage() {
  const { projectHumanId } = useParams();
  const [learning, setLearning] = useState<LearningResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adminError, setAdminError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [memoryId, setMemoryId] = useState("");
  const [action, setAction] = useState<"retire" | "supersede">("retire");
  const [actor, setActor] = useState("");
  const [reason, setReason] = useState("");
  const [successorTitle, setSuccessorTitle] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  async function load(id: string) {
    const body = await api.projectLearning(id);
    setLearning(body);
    setError(null);
    if (!memoryId && body.active_memories[0]) {
      setMemoryId(body.active_memories[0].memory_human_id);
    }
  }

  useEffect(() => {
    if (!projectHumanId) {
      return;
    }
    const id = projectHumanId;
    let cancelled = false;
    async function run() {
      try {
        const body = await api.projectLearning(id);
        if (!cancelled) {
          setLearning(body);
          setError(null);
          setMemoryId((current) => current || body.active_memories[0]?.memory_human_id || "");
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load learning");
        }
      }
    }
    void run();
    return () => {
      cancelled = true;
    };
  }, [projectHumanId]);

  async function onAdmin(event: FormEvent) {
    event.preventDefault();
    if (!projectHumanId || !memoryId) {
      return;
    }
    setBusy(true);
    setAdminError(null);
    try {
      if (action === "retire") {
        await api.retireMemory(projectHumanId, memoryId, {
          confirmed,
          reason,
          actor,
        });
      } else {
        await api.supersedeMemory(projectHumanId, memoryId, {
          confirmed,
          reason,
          actor,
          successor_title: successorTitle,
        });
      }
      setConfirmed(false);
      setReason("");
      setSuccessorTitle("");
      await load(projectHumanId);
    } catch (err) {
      setAdminError(err instanceof ApiError ? err.message : "Memory administration failed");
    } finally {
      setBusy(false);
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
  if (!learning) {
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <p className="muted">Loading learning…</p>
      </section>
    );
  }

  return (
    <section className="page">
      <ProjectNav projectHumanId={projectHumanId} />
      <h1>Organizational learning</h1>
      <p className="badge live">Auto-learned AGENT_MEMORY</p>
      <p className="muted">{learning.notice}</p>
      <p className="muted">Ordinary promotion is automatic. There is no approval control here.</p>

      <h2>ACTIVE memories</h2>
      {learning.active_memories.length === 0 ? (
        <p className="muted">No ACTIVE memories</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>MEM ID</th>
              <th>Agent</th>
              <th>Title</th>
              <th>Evidence</th>
              <th>Confidence</th>
              <th>Occurrences</th>
              <th>Last validated</th>
              <th>Status</th>
              <th>Promotion</th>
            </tr>
          </thead>
          <tbody>
            {learning.active_memories.map((item) => (
              <tr key={item.memory_human_id}>
                <td>{item.memory_human_id}</td>
                <td>{item.agent_role}</td>
                <td>{item.title}</td>
                <td>{dtoText(item.evidence_ref)}</td>
                <td>{item.confidence.toFixed(2)}</td>
                <td>{item.occurrence_count}</td>
                <td>{dtoText(item.last_validated_at)}</td>
                <td>{item.status}</td>
                <td>auto-learned</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <MemoryList heading="Rejected memories" items={learning.rejected_memories} empty="No rejected memories" />
      <MemoryList heading="Retired memories" items={learning.retired_memories || []} empty="No retired memories" />
      <MemoryList
        heading="Superseded memories"
        items={learning.superseded_memories || []}
        empty="No superseded memories"
      />

      <h2>Promoted / reinforced / rejected / admin events</h2>
      {learning.events.length === 0 ? (
        <p className="muted">No learning events</p>
      ) : (
        <ul className="plain-list">
          {learning.events.map((event, index) => (
            <li key={`${event.memory_human_id}-${event.created_at}-${index}`}>
              {event.created_at} · {event.event_type} · {event.memory_human_id}
              {event.actor ? ` · actor ${event.actor}` : ""}
              {event.rejection_code ? ` · ${event.rejection_code}: ${dtoText(event.rejection_reason)}` : ""}
            </li>
          ))}
        </ul>
      )}

      <h2>Memories injected into recent runs</h2>
      {learning.injected_in_recent_runs.length === 0 ? (
        <p className="muted">No injections recorded</p>
      ) : (
        <ul className="plain-list">
          {learning.injected_in_recent_runs.map((item, index) => (
            <li key={`${item.job_human_id}-${item.memory_human_id}-${index}`}>
              {item.created_at} · job {item.job_human_id} · {item.memory_human_id}
            </li>
          ))}
        </ul>
      )}

      <h2>Governed administration</h2>
      <p className="muted">
        Retire or supersede an ACTIVE memory. Confirmation, reason, and actor are required.
        History is preserved. Direct Markdown editing is not supported.
      </p>
      <form className="intake-form" onSubmit={(event) => void onAdmin(event)}>
        <label>
          Memory
          <select value={memoryId} onChange={(event) => setMemoryId(event.target.value)} required>
            <option value="">Select an ACTIVE memory</option>
            {learning.active_memories.map((item) => (
              <option key={item.memory_human_id} value={item.memory_human_id}>
                {item.memory_human_id} · {item.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          Action
          <select
            value={action}
            onChange={(event) => setAction(event.target.value as "retire" | "supersede")}
          >
            <option value="retire">Retire</option>
            <option value="supersede">Supersede</option>
          </select>
        </label>
        {action === "supersede" ? (
          <label>
            Successor title
            <input
              value={successorTitle}
              onChange={(event) => setSuccessorTitle(event.target.value)}
              required
            />
          </label>
        ) : null}
        <label>
          Actor
          <input value={actor} onChange={(event) => setActor(event.target.value)} required />
        </label>
        <label>
          Reason
          <input value={reason} onChange={(event) => setReason(event.target.value)} required />
        </label>
        <label>
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
          />{" "}
          I confirm this governed change
        </label>
        {adminError ? <div className="banner error">{adminError}</div> : null}
        <div className="intake-actions">
          <button type="submit" disabled={busy || learning.active_memories.length === 0}>
            {busy ? "Submitting…" : "Apply"}
          </button>
        </div>
      </form>
    </section>
  );
}
