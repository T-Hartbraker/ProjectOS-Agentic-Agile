import { FormEvent, useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import type { OpenAISettingsResponse } from "../api/types";
import { TechnicalDetails } from "../components/TechnicalDetails";
import {
  apiKeyStatusLabel,
  normalizeOpenAISettings,
  sourceLabel,
  testStatusLabel,
} from "./openaiSettingsHelpers";

function dtoText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "Not reported";
  }
  return String(value);
}

type LoadState = "loading" | "ready" | "error";

export function OpenAISettingsPage() {
  const [settings, setSettings] = useState<OpenAISettingsResponse | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [chatgptUserId, setChatgptUserId] = useState("");
  const [keyNotice, setKeyNotice] = useState<string | null>(null);
  const [modelNotice, setModelNotice] = useState<string | null>(null);
  const [bridgeNotice, setBridgeNotice] = useState<string | null>(null);

  const reload = useCallback(async () => {
    const body = normalizeOpenAISettings(await api.openaiSettings());
    setSettings(body);
    setModel(body.model);
    setChatgptUserId(body.slack_chatgpt_user_id);
    setLoadState("ready");
    setError(null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoadState("loading");
      try {
        await reload();
      } catch (err) {
        if (!cancelled) {
          setLoadState("error");
          setError(
            err instanceof ApiError
              ? err.message
              : "Unable to load OpenAI integration settings.",
          );
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [reload]);

  async function onSaveKey(event: FormEvent) {
    event.preventDefault();
    const key = apiKey.trim();
    if (!key) {
      setFormError("Enter an API key to save.");
      return;
    }
    setBusy(true);
    setFormError(null);
    setKeyNotice(null);
    try {
      const result = await api.saveOpenAISecret({ api_key: key });
      setApiKey("");
      setKeyNotice(result.notice);
      await reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not save OpenAI API key");
    } finally {
      setBusy(false);
    }
  }

  async function onRemoveKey() {
    setBusy(true);
    setFormError(null);
    setKeyNotice(null);
    try {
      const result = await api.removeOpenAISecret();
      setApiKey("");
      setKeyNotice(result.notice);
      await reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not remove OpenAI API key");
    } finally {
      setBusy(false);
    }
  }

  async function onTestConnection() {
    setBusy(true);
    setFormError(null);
    setKeyNotice(null);
    try {
      const result = await api.testOpenAIConnection();
      setKeyNotice(result.detail || (result.ok ? "Connection successful" : "Connection failed"));
      await reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Connection test failed");
    } finally {
      setBusy(false);
    }
  }

  async function onSaveBridge(event: FormEvent) {
    event.preventDefault();
    const userId = chatgptUserId.trim();
    if (!userId) {
      setFormError("Enter the Slack user ID for the installed ChatGPT app.");
      return;
    }
    setBusy(true);
    setFormError(null);
    setBridgeNotice(null);
    try {
      await api.updateOpenAISettings({ slack_chatgpt_user_id: userId });
      setBridgeNotice("Slack ChatGPT trigger user ID updated.");
      await reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not update Slack bridge settings");
    } finally {
      setBusy(false);
    }
  }

  async function onSaveModel(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    setModelNotice(null);
    try {
      await api.updateOpenAISettings({ model });
      setModelNotice("Model updated.");
      await reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Could not update model");
    } finally {
      setBusy(false);
    }
  }

  if (loadState === "loading") {
    return <p className="muted">Loading OpenAI settings…</p>;
  }

  if (loadState === "error") {
    return (
      <div className="banner error">
        <p>{error || "Unable to load OpenAI integration settings."}</p>
        <button type="button" onClick={() => void reload().catch(() => undefined)}>
          Retry
        </button>
      </div>
    );
  }

  if (!settings) {
    return (
      <div className="banner error">
        <p>Unable to load OpenAI integration settings.</p>
        <button type="button" onClick={() => void reload().catch(() => undefined)}>
          Retry
        </button>
      </div>
    );
  }

  return (
    <>
      <h2>OpenAI / ChatGPT Advisor</h2>
      <p className="muted">
        ChatGPT Advisor runs inside the existing ProjectOS Slack app. Enter your OpenAI API key
        once below — it is saved securely on this machine and persists across ProjectOS updates.
      </p>

      {formError ? <div className="banner error">{formError}</div> : null}

      <div className="narrative-grid">
        <article className="card">
          <h2>API key</h2>
          <p className="stat">{apiKeyStatusLabel(settings)}</p>
          <p className="muted">Source: {sourceLabel(settings.api_key_source)}</p>
        </article>
        <article className="card">
          <h2>Model</h2>
          <p>{settings.model}</p>
        </article>
        <article className="card">
          <h2>Last connection test</h2>
          <p className="stat">{testStatusLabel(settings)}</p>
          {settings.last_error ? <p className="muted">{settings.last_error}</p> : null}
        </article>
      </div>

      <h2>OpenAI API key</h2>
      <p className="muted">
        Save your OpenAI API key here once. ProjectOS stores it in an encrypted file under its
        state folder on this PC. It is not kept in the browser or in git.{" "}
        <code>PROJECTOS_OPENAI_API_KEY</code> overrides the stored key when set.
      </p>
      <form className="intake-form" onSubmit={(event) => void onSaveKey(event)}>
        <label>
          OpenAI API key (sk-)
          <input
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder={settings.api_key_configured ? "Replace API key" : "sk-..."}
          />
        </label>
        {keyNotice ? <p className="banner">{keyNotice}</p> : null}
        <div className="intake-actions">
          <button type="submit" disabled={busy}>
            {busy ? "Saving…" : settings.api_key_configured ? "Update key" : "Save key"}
          </button>
          {settings.api_key_configured ? (
            <>
              <button type="button" disabled={busy} onClick={() => void onRemoveKey()}>
                Remove key
              </button>
              <button type="button" disabled={busy} onClick={() => void onTestConnection()}>
                Test connection
              </button>
            </>
          ) : null}
        </div>
      </form>

      <h2>Slack bridge</h2>
      <p className="muted">
        ProjectOS treats a mention of this Slack user ID as the ChatGPT Advisor trigger. Mention the
        installed ChatGPT app in an authorized interface channel (for example{" "}
        <code>&lt;@U0BTHBJK51A&gt;</code>). ProjectOS routes the thread; the ChatGPT app itself does
        not need to respond.
      </p>
      <form className="intake-form" onSubmit={(event) => void onSaveBridge(event)}>
        <label>
          ChatGPT app user ID
          <input
            value={chatgptUserId}
            onChange={(event) => setChatgptUserId(event.target.value)}
            placeholder="U0BTHBJK51A"
            disabled={busy}
          />
        </label>
        {bridgeNotice ? <p className="banner">{bridgeNotice}</p> : null}
        <div className="intake-actions">
          <button
            type="submit"
            disabled={busy || chatgptUserId.trim() === settings.slack_chatgpt_user_id}
          >
            {busy ? "Saving…" : "Save Slack trigger"}
          </button>
        </div>
      </form>

      <h2>Model</h2>
      <p className="muted">Choose the Responses API model used by ChatGPT Advisor in Slack.</p>
      <form className="intake-form" onSubmit={(event) => void onSaveModel(event)}>
        <label>
          Responses API model
          <select value={model} onChange={(event) => setModel(event.target.value)} disabled={busy}>
            {settings.supported_models.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        {modelNotice ? <p className="banner">{modelNotice}</p> : null}
        <div className="intake-actions">
          <button type="submit" disabled={busy || model === settings.model}>
            {busy ? "Saving…" : "Save model"}
          </button>
        </div>
      </form>

      <h2>Setup</h2>
      <ol className="plain-list">
        {settings.setup_steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>

      <TechnicalDetails title="Technical details">
        <p>enabled {String(settings.enabled)}</p>
        <p>api key source {settings.api_key_source}</p>
        <p>api key configured {String(settings.api_key_configured)}</p>
        <p>slack chatgpt user {dtoText(settings.slack_chatgpt_user_id)}</p>
        <p>slack chatgpt user source {dtoText(settings.slack_chatgpt_user_id_source)}</p>
        <p>last test at {dtoText(settings.last_test_at)}</p>
      </TechnicalDetails>
    </>
  );
}
