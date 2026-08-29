import { describe, expect, it } from "vitest";
import {
  apiKeyStatusLabel,
  normalizeOpenAISettings,
  sourceLabel,
  testStatusLabel,
} from "./openaiSettingsHelpers";

describe("normalizeOpenAISettings", () => {
  it("fills defaults for partial API payloads", () => {
    const normalized = normalizeOpenAISettings({
      enabled: true,
      api_key_configured: true,
      api_key_source: "encrypted_store",
      model: "gpt-4.1-mini",
    });
    expect(normalized.slack_chatgpt_user_id).toBe("");
    expect(normalized.supported_models).toEqual(["gpt-4.1-mini"]);
    expect(normalized.setup_steps).toEqual([]);
  });

  it("normalizes encrypted_local_store to encrypted_store", () => {
    const normalized = normalizeOpenAISettings({
      api_key_source: "encrypted_local_store" as never,
    });
    expect(normalized.api_key_source).toBe("encrypted_store");
  });
});

describe("openai settings labels", () => {
  it("reports configured and source labels", () => {
    const settings = normalizeOpenAISettings({
      api_key_configured: true,
      api_key_source: "encrypted_store",
      last_test_status: "success",
    });
    expect(apiKeyStatusLabel(settings)).toBe("Configured");
    expect(sourceLabel(settings.api_key_source)).toBe("Encrypted local store");
    expect(testStatusLabel(settings)).toBe("Success");
  });
});
