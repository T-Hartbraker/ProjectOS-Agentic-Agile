import type { OpenAISettingsResponse } from "../api/types";

export function normalizeOpenAISettings(
  body: Partial<OpenAISettingsResponse> | null | undefined,
): OpenAISettingsResponse {
  const source = body?.api_key_source;
  const normalizedSource =
    source === "environment" || source === "encrypted_store" || source === "none"
      ? source
      : source === "encrypted_local_store"
        ? "encrypted_store"
        : "none";
  return {
    enabled: Boolean(body?.enabled),
    api_key_configured: Boolean(body?.api_key_configured),
    api_key_source: normalizedSource,
    model: String(body?.model || "gpt-4.1-mini"),
    supported_models: Array.isArray(body?.supported_models)
      ? body.supported_models.map(String)
      : ["gpt-4.1-mini"],
    slack_chatgpt_user_id: String(body?.slack_chatgpt_user_id || ""),
    slack_chatgpt_user_id_source: String(body?.slack_chatgpt_user_id_source || "default"),
    last_test_status: body?.last_test_status ?? null,
    last_test_at: body?.last_test_at ?? null,
    last_error: body?.last_error ?? null,
    setup_steps: Array.isArray(body?.setup_steps) ? body.setup_steps.map(String) : [],
  };
}

export function apiKeyStatusLabel(settings: OpenAISettingsResponse): string {
  return settings.api_key_configured ? "Configured" : "Not configured";
}

export function sourceLabel(source: OpenAISettingsResponse["api_key_source"]): string {
  if (source === "environment") return "Environment variable";
  if (source === "encrypted_store") return "Encrypted local store";
  return "None";
}

export function testStatusLabel(settings: OpenAISettingsResponse): string {
  if (settings.last_test_status === "success") return "Success";
  if (settings.last_test_status === "failed") return "Failed";
  return "Not tested";
}
