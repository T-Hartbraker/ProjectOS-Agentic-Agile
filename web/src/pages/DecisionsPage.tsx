import { FormEvent, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { DecisionListResponse, DecisionResponse } from "../api/types";
import { ProjectNav } from "../components/ProjectNav";

const ACTIONS = [
  { value: "sponsor_reserved", label: "Sponsor-reserved decision" },
  { value: "release_approve", label: "Release approval" },
  { value: "cancel_job", label: "Cancel job" },
  { value: "recover_salvage", label: "Destructive salvage" },
  { value: "recover_reconcile", label: "Destructive release reconcile" },
  { value: "governance_change", label: "Material governance change" },
];

function dtoText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "Not reported";
  }
  return String(value);
}

export function DecisionsPage() {
  const { projectHumanId } = useParams();
  const [payload, setPayload] = useState<DecisionListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [action, setAction] = useState("sponsor_reserved");
  const [targetKind, setTargetKind] = useState("none");
  const [targetHumanId, setTargetHumanId] = useState("");
  const [reason, setReason] = useState("");
  const [impact, setImpact] = useState("");
  const [requestedBy, setRequestedBy] = useState("");
  const [decisionActor, setDecisionActor] = useState("");
  const [decisionReason, setDecisionReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  async function reload(id: string) {
    const body = await api.projectDecisions(id);
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
          setError(err instanceof ApiError ? err.message : "Failed to load decisions");
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectHumanId]);

  async function onOpen(event: FormEvent) {
    event.preventDefault();
    if (!projectHumanId) {
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      await api.openDecision(projectHumanId, {
        action,
        reason,
        impact,
        requested_by: requestedBy,
        target_kind: targetKind,
        target_human_id: targetHumanId.trim() || null,
      });
      setReason("");
      setImpact("");
      setTargetHumanId("");
      setConfirmed(false);
      await reload(projectHumanId);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not open decision");
    } finally {
      setBusy(false);
    }
  }

  async function resolve(item: DecisionResponse, kind: "approve" | "reject") {
    if (!projectHumanId) {
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      const body = {
        confirmed,
        actor: decisionActor,
        reason: decisionReason,
      };
      if (kind === "approve") {
        await api.approveDecision(projectHumanId, item.decision_human_id, body);
      } else {
        await api.rejectDecision(projectHumanId, item.decision_human_id, body);
      }
      setConfirmed(false);
      setDecisionReason("");
      await reload(projectHumanId);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Decision was not recorded");
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
  if (!payload) {
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <p className="muted">Loading decisions…</p>
      </section>
    );
  }

  const openItems = payload.decisions.filter((item) => item.status === "OPEN");

  return (
    <section className="page">
      <ProjectNav projectHumanId={projectHumanId} />
      <h1>Sponsor decisions</h1>
      <p className="badge live">Explicit grant required</p>
      <p className="muted">{payload.notice}</p>
      <p className="muted">
        Natural-language chat does not approve work. A Sponsor must use Approve or Reject
        below, with confirmation, actor, and reason.
      </p>

      <h2>Open requests</h2>
      {openItems.length === 0 ? (
        <p className="muted">No OPEN decisions</p>
      ) : (
        <ul className="plain-list">
          {openItems.map((item) => (
            <li key={item.decision_human_id}>
              <strong>{item.decision_human_id}</strong> · {item.action} · target{" "}
              {dtoText(item.target_human_id)} · requested by {item.requested_by}
              <p className="muted">{item.reason}</p>
              <p className="muted">Impact: {item.impact}</p>
              <div className="intake-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void resolve(item, "approve")}
                >
                  Approve
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void resolve(item, "reject")}
                >
                  Reject
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <h2>Resolve with confirmation</h2>
      <form className="intake-form" onSubmit={(event) => event.preventDefault()}>
        <label>
          Sponsor actor
          <input value={decisionActor} onChange={(event) => setDecisionActor(event.target.value)} />
        </label>
        <label>
          Decision reason
          <input
            value={decisionReason}
            onChange={(event) => setDecisionReason(event.target.value)}
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
          />{" "}
          I confirm this Sponsor decision
        </label>
      </form>

      <h2>Open a request</h2>
      <form className="intake-form" onSubmit={(event) => void onOpen(event)}>
        <label>
          Action
          <select value={action} onChange={(event) => setAction(event.target.value)}>
            {ACTIONS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Target kind
          <select value={targetKind} onChange={(event) => setTargetKind(event.target.value)}>
            <option value="none">none</option>
            <option value="job">job</option>
            <option value="release">release</option>
            <option value="project">project</option>
          </select>
        </label>
        <label>
          Target ID
          <input
            value={targetHumanId}
            onChange={(event) => setTargetHumanId(event.target.value)}
          />
        </label>
        <label>
          Requested by
          <input value={requestedBy} onChange={(event) => setRequestedBy(event.target.value)} />
        </label>
        <label>
          Reason
          <input value={reason} onChange={(event) => setReason(event.target.value)} />
        </label>
        <label>
          Impact
          <input value={impact} onChange={(event) => setImpact(event.target.value)} />
        </label>
        {formError ? <div className="banner error">{formError}</div> : null}
        <div className="intake-actions">
          <button type="submit" disabled={busy}>
            {busy ? "Submitting…" : "Open decision"}
          </button>
        </div>
      </form>

      <h2>Audit history</h2>
      {payload.decisions.length === 0 ? (
        <p className="muted">No decision records</p>
      ) : (
        <ul className="plain-list">
          {payload.decisions.map((item) => (
            <li key={`${item.decision_human_id}-history`}>
              {item.decision_human_id} · {item.status} · {item.action} ·{" "}
              {dtoText(item.decided_by)}
              {item.events.map((event, index) => (
                <p className="muted" key={`${item.decision_human_id}-${index}`}>
                  {event.created_at} · {event.event_type} · {dtoText(event.actor)} ·{" "}
                  {dtoText(event.reason)}
                </p>
              ))}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
