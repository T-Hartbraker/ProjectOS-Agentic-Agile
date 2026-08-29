import { FormEvent, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { SlackBindingListResponse, SlackNotification, SlackStatusResponse } from "../api/types";
import { ProjectNav } from "../components/ProjectNav";
import { TechnicalDetails } from "../components/TechnicalDetails";

function dtoText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "Not reported";
  }
  return String(value);
}

function statusLabel(status: string): string {
  if (status === "connected") {
    return "Connected";
  }
  if (status === "not_configured") {
    return "Not configured";
  }
  if (status === "disabled") {
    return "Disabled";
  }
  if (status === "connecting") {
    return "Connecting";
  }
  if (status === "disconnected") {
    return "Disconnected";
  }
  if (status === "error") {
    return "Error";
  }
  return status;
}

export function SlackPage() {
  const { projectHumanId } = useParams();
  const [payload, setPayload] = useState<SlackBindingListResponse | null>(null);
  const [status, setStatus] = useState<SlackStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [channelId, setChannelId] = useState("");
  const [teamId, setTeamId] = useState("");
  const [threadTs, setThreadTs] = useState("");
  const [notifications, setNotifications] = useState<SlackNotification[]>([]);
  const [notifyNotice, setNotifyNotice] = useState<string | null>(null);

  async function reload(id: string) {
    const [body, notices, slack] = await Promise.all([
      api.projectSlack(id),
      api.projectSlackNotifications(id),
      api.slackStatus(),
    ]);
    setPayload(body);
    setNotifications(notices.notifications);
    setStatus(slack);
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
          setError(err instanceof ApiError ? err.message : "Failed to load Slack bindings");
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectHumanId]);

  async function onBind(event: FormEvent) {
    event.preventDefault();
    if (!projectHumanId) {
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      await api.bindSlack(projectHumanId, {
        channel_id: channelId,
        team_id: teamId.trim() || null,
        thread_ts: threadTs.trim() || null,
      });
      setChannelId("");
      setTeamId("");
      setThreadTs("");
      await reload(projectHumanId);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not bind Slack location");
    } finally {
      setBusy(false);
    }
  }

  async function onNotify() {
    if (!projectHumanId) {
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      const result = await api.notifySlack(projectHumanId);
      setNotifyNotice(
        `${result.notice} Posted ${result.posted.length}; already posted ${result.already_posted.length}.`,
      );
      await reload(projectHumanId);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not post Slack notices");
    } finally {
      setBusy(false);
    }
  }

  async function onUnbind(channel: string, team: string | null, thread: string | null) {
    if (!projectHumanId) {
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      await api.unbindSlack(projectHumanId, {
        channel_id: channel,
        team_id: team,
        thread_ts: thread,
      });
      await reload(projectHumanId);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not unbind Slack location");
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
  if (!payload || !status) {
    return (
      <section className="page">
        <ProjectNav projectHumanId={projectHumanId} />
        <p className="muted">Loading Slack setup…</p>
      </section>
    );
  }

  const bound = status.bound_channel;
  const thisProjectBindings = payload.bindings;

  return (
    <section className="page">
      <ProjectNav projectHumanId={projectHumanId} />
      <h1>Slack</h1>
      <p className="muted">
        Slack talks to this PC through Socket Mode. No public URL or tunnel is required.
        Global interface channels are configured in Settings → Integrations → Slack.
        Per-project bindings below remain supported for legacy routing.
      </p>

      <div className="narrative-grid">
        <article className="card">
          <h2>Slack connection</h2>
          <p className="stat">{statusLabel(status.connection_status)}</p>
          <p className="muted">{status.detail || "Socket Mode is the local default."}</p>
        </article>
        <article className="card">
          <h2>App token</h2>
          <p>{status.app_token === "missing" ? "Missing" : "Configured"}</p>
        </article>
        <article className="card">
          <h2>Bot token</h2>
          <p>{status.bot_token === "missing" ? "Missing" : "Configured"}</p>
        </article>
        <article className="card">
          <h2>Workspace</h2>
          <p>{dtoText(status.workspace_name)}</p>
        </article>
        <article className="card">
          <h2>Bound channel</h2>
          <p>
            {thisProjectBindings.length === 0
              ? "None yet"
              : thisProjectBindings
                  .map((item) => item.channel_id)
                  .join(", ")}
          </p>
        </article>
        <article className="card">
          <h2>Project</h2>
          <p>{projectHumanId}</p>
        </article>
      </div>

      <h2>First-time setup</h2>
      <ol className="plain-list">
        {status.setup_steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>

      <h2>Bind this project to a Slack channel</h2>
      <p className="muted">
        Slack Channel ID looks like C0123456789. Team ID looks like T0123456789.
        Unbound channels are rejected.
      </p>
      <form className="intake-form" onSubmit={(event) => void onBind(event)}>
        <label>
          Channel ID
          <input value={channelId} onChange={(event) => setChannelId(event.target.value)} required />
        </label>
        <label>
          Team ID (recommended)
          <input value={teamId} onChange={(event) => setTeamId(event.target.value)} />
        </label>
        <label>
          Thread timestamp (optional)
          <input value={threadTs} onChange={(event) => setThreadTs(event.target.value)} />
        </label>
        {formError ? <div className="banner error">{formError}</div> : null}
        <div className="intake-actions">
          <button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Bind to this project"}
          </button>
        </div>
      </form>

      <h2>Delivery notices</h2>
      <p className="muted">
        Posts only iteration review, Sponsor decision, blocking QA, release, and significant
        recovery events. Duplicate notices are not sent again.
      </p>
      <div className="intake-actions">
        <button type="button" disabled={busy} onClick={() => void onNotify()}>
          {busy ? "Scanning…" : "Scan and notify"}
        </button>
      </div>
      {notifyNotice ? <p className="muted">{notifyNotice}</p> : null}
      {notifications.length === 0 ? (
        <p className="muted">No delivery notices posted</p>
      ) : (
        <ul className="plain-list">
          {notifications.map((item) => (
            <li key={item.notification_human_id}>
              {item.kind} · {item.entity_human_id} · {item.dashboard_path}
            </li>
          ))}
        </ul>
      )}

      <h2>Bindings</h2>
      {payload.bindings.length === 0 ? (
        <p className="muted">No Slack bindings</p>
      ) : (
        <ul className="plain-list">
          {payload.bindings.map((item) => (
            <li key={item.binding_human_id}>
              Channel {item.channel_id}
              <div className="intake-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void onUnbind(item.channel_id, item.team_id, item.thread_ts)}
                >
                  Unbind
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <TechnicalDetails title="Technical details">
        <p>mode {status.mode}</p>
        <p>app token {status.app_token}</p>
        <p>bot token {status.bot_token}</p>
        <p>team {dtoText(status.team_id)}</p>
        <p>workspace {dtoText(status.workspace_name)}</p>
        {bound ? (
          <p>
            bound {bound.project_human_id} / {bound.channel_id}
          </p>
        ) : null}
        {payload.bindings.map((item) => (
          <p key={item.binding_human_id}>
            {item.binding_human_id} · channel {item.channel_id} · team {dtoText(item.team_id)} ·
            thread {dtoText(item.thread_ts)}
          </p>
        ))}
      </TechnicalDetails>
    </section>
  );
}
