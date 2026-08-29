import { FormEvent, useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import type { SlackSettingsResponse } from "../api/types";
import { TechnicalDetails } from "../components/TechnicalDetails";

function dtoText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "Not reported";
  }
  return String(value);
}

function statusLabel(status: string): string {
  if (status === "connected") return "Connected";
  if (status === "not_configured") return "Not configured";
  if (status === "disabled") return "Disabled";
  if (status === "connecting") return "Connecting";
  if (status === "disconnected") return "Disconnected";
  if (status === "error") return "Error";
  return status;
}

export function SlackSettingsPage() {
  const [settings, setSettings] = useState<SlackSettingsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [channelId, setChannelId] = useState("");
  const [teamId, setTeamId] = useState("");
  const [appToken, setAppToken] = useState("");
  const [botToken, setBotToken] = useState("");
  const [signingSecret, setSigningSecret] = useState("");
  const [tokenNotice, setTokenNotice] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);

  async function reload() {
    const body = await api.slackSettings();
    setSettings(body);
    setError(null);
  }

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        await reload();
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load Slack settings");
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  async function onSaveTokens(event: FormEvent) {
    event.preventDefault();
    const payload: {
      app_token?: string;
      bot_token?: string;
      signing_secret?: string;
    } = {};
    const app = appToken.trim();
    const bot = botToken.trim();
    const signing = signingSecret.trim();
    if (app) {
      payload.app_token = app;
    }
    if (bot) {
      payload.bot_token = bot;
    }
    if (signing) {
      payload.signing_secret = signing;
    }
    if (!payload.app_token && !payload.bot_token && !payload.signing_secret) {
      setFormError("Enter at least one token to save.");
      return;
    }
    setBusy(true);
    setFormError(null);
    setTokenNotice(null);
    try {
      const result = await api.saveSlackTokens(payload);
      setAppToken("");
      setBotToken("");
      setSigningSecret("");
      setTokenNotice(result.notice);
      await reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not save Slack tokens");
    } finally {
      setBusy(false);
    }
  }

  async function onTestConnection() {
    setBusy(true);
    setFormError(null);
    setTestResult(null);
    try {
      const result = await api.testSlackConnection();
      setTestResult(
        `App token: ${result.app_token}; Bot token: ${result.bot_token}; Socket Mode: ${result.socket_mode}; ${result.detail}`,
      );
      await reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Slack connection test failed");
    } finally {
      setBusy(false);
    }
  }

  async function onAddChannel(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      await api.updateSlackSettings({
        add_interface_channels: [
          {
            channel_id: channelId.trim(),
            team_id: teamId.trim() || null,
            is_default: (settings?.interface_channels.length ?? 0) === 0,
          },
        ],
      });
      setChannelId("");
      setTeamId("");
      await reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not add interface channel");
    } finally {
      setBusy(false);
    }
  }

  async function onRemove(channel: string, team: string | null) {
    setBusy(true);
    setFormError(null);
    try {
      await api.updateSlackSettings({
        remove_interface_channels: [{ channel_id: channel, team_id: team }],
      });
      await reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not remove interface channel");
    } finally {
      setBusy(false);
    }
  }

  async function onSetDefault(channel: string, team: string | null) {
    setBusy(true);
    setFormError(null);
    try {
      await api.updateSlackSettings({
        default_channel_id: channel,
        default_team_id: team,
      });
      await reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not set default channel");
    } finally {
      setBusy(false);
    }
  }

  if (error) {
    return <div className="banner error">{error}</div>;
  }
  if (!settings) {
    return <p className="muted">Loading Slack settings…</p>;
  }

  return (
    <>
      <h2>Slack integration</h2>
      <p className="muted">
        Socket Mode connects outbound from this PC. Enter your Slack tokens once below — they are
        saved securely on this machine and persist across ProjectOS updates.
      </p>

      {formError ? <div className="banner error">{formError}</div> : null}

      <div className="narrative-grid">
        <article className="card">
          <h2>Connection</h2>
          <p className="stat">{statusLabel(settings.connection_status)}</p>
          <p className="muted">{settings.detail || "Socket Mode transport"}</p>
        </article>
        <article className="card">
          <h2>Transport</h2>
          <p>{settings.transport === "socket" ? "Socket Mode" : settings.transport}</p>
        </article>
        <article className="card">
          <h2>App token</h2>
          <p>{settings.app_token_present ? "Configured" : "Missing"}</p>
          <p className="muted">Source: {settings.app_token_source ?? "none"}</p>
          {!settings.app_token_valid_prefix && settings.app_token_present ? (
            <p className="muted">Prefix should be xapp-</p>
          ) : null}
        </article>
        <article className="card">
          <h2>Bot token</h2>
          <p>{settings.bot_token_present ? "Configured" : "Missing"}</p>
          <p className="muted">Source: {settings.bot_token_source ?? "none"}</p>
          {!settings.bot_token_valid_prefix && settings.bot_token_present ? (
            <p className="muted">Prefix should be xoxb-</p>
          ) : null}
        </article>
        <article className="card">
          <h2>Signing secret</h2>
          <p>{settings.signing_secret_present ? "Configured" : "Optional"}</p>
        </article>
        <article className="card">
          <h2>Workspace</h2>
          <p>{dtoText(settings.workspace_name)}</p>
        </article>
        <article className="card">
          <h2>Default channel</h2>
          <p>{dtoText(settings.default_channel_id)}</p>
        </article>
      </div>

      <h2>Slack tokens</h2>
      <p className="muted">
        Save your app and bot tokens here once. ProjectOS stores them in an encrypted file under
        its state folder on this PC. They are not kept in the browser or in git.
      </p>
      <form className="intake-form" onSubmit={(event) => void onSaveTokens(event)}>
        <label>
          App-level token (xapp-)
          <input
            type="password"
            autoComplete="off"
            value={appToken}
            onChange={(event) => setAppToken(event.target.value)}
            placeholder={settings.app_token_present ? "Replace app token" : "xapp-..."}
          />
        </label>
        <label>
          Bot token (xoxb-)
          <input
            type="password"
            autoComplete="off"
            value={botToken}
            onChange={(event) => setBotToken(event.target.value)}
            placeholder={settings.bot_token_present ? "Replace bot token" : "xoxb-..."}
          />
        </label>
        <label>
          Signing secret (optional, HTTP slash only)
          <input
            type="password"
            autoComplete="off"
            value={signingSecret}
            onChange={(event) => setSigningSecret(event.target.value)}
            placeholder={settings.signing_secret_present ? "Replace signing secret" : "Optional"}
          />
        </label>
        {tokenNotice ? <p className="banner">{tokenNotice}</p> : null}
        {testResult ? <p className="banner">{testResult}</p> : null}
        <div className="intake-actions">
          <button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Save tokens"}
          </button>
          <button type="button" disabled={busy} onClick={() => void onTestConnection()}>
            {busy ? "Testing…" : "Test connection"}
          </button>
        </div>
      </form>

      <h2>Global interface channels</h2>
      <p className="muted">
        Channels listed here can reach ProjectOS without a per-project binding. Use your
        #projectos channel ID (for example C0123456789).
      </p>
      {settings.interface_channels.length === 0 ? (
        <p className="muted">No interface channels configured yet.</p>
      ) : (
        <ul className="plain-list">
          {settings.interface_channels.map((item) => (
            <li key={`${item.team_id ?? ""}:${item.channel_id}`}>
              {item.channel_id}
              {item.is_default ? " (default)" : ""}
              <div className="intake-actions">
                {!item.is_default ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void onSetDefault(item.channel_id, item.team_id)}
                  >
                    Set default
                  </button>
                ) : null}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void onRemove(item.channel_id, item.team_id)}
                >
                  Remove
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <form className="intake-form" onSubmit={(event) => void onAddChannel(event)}>
        <label>
          Channel ID
          <input value={channelId} onChange={(event) => setChannelId(event.target.value)} required />
        </label>
        <label>
          Team ID (recommended)
          <input value={teamId} onChange={(event) => setTeamId(event.target.value)} />
        </label>
        <div className="intake-actions">
          <button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Add interface channel"}
          </button>
        </div>
      </form>

      <h2>Setup</h2>
      <ol className="plain-list">
        {settings.setup_steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>

      {settings.bound_channels.length > 0 ? (
        <>
          <h2>Legacy bound channels</h2>
          <p className="muted">
            Per-project bindings still work. Manage them from each project&apos;s Slack page.
          </p>
          <ul className="plain-list">
            {settings.bound_channels.map((item) => (
              <li key={`${item.project_human_id}:${item.channel_id}`}>
                {item.project_human_id} → {item.channel_id}
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <TechnicalDetails title="Technical details">
        <p>enabled {String(settings.enabled)}</p>
        <p>mode {settings.mode}</p>
        <p>app token {settings.app_token}</p>
        <p>bot token {settings.bot_token}</p>
        <p>storage {settings.storage ?? "none"}</p>
        <p>configured {String(settings.configured ?? (settings.app_token_present && settings.bot_token_present))}</p>
        <p>team {dtoText(settings.team_id)}</p>
        <p>connection updated {dtoText(settings.connection_updated_at)}</p>
      </TechnicalDetails>
    </>
  );
}
