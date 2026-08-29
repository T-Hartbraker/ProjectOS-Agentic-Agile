import { afterEach, describe, expect, it } from "vitest";
import {
  DISPLAY_SCALE_KEY,
  DISPLAY_SCALES,
  loadDisplayScale,
  parseDisplayScale,
  saveDisplayScale,
} from "./displayScale";

describe("display scale", () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.style.removeProperty("--scale");
  });

  it("parses compact, standard, and large", () => {
    expect(parseDisplayScale("compact")).toBe("compact");
    expect(parseDisplayScale("large")).toBe("large");
    expect(parseDisplayScale("nope")).toBe("standard");
    expect(DISPLAY_SCALES.compact).toBe(0.9);
    expect(DISPLAY_SCALES.standard).toBe(1);
    expect(DISPLAY_SCALES.large).toBe(1.15);
  });

  it("persists the preference in localStorage", () => {
    saveDisplayScale("large");
    expect(window.localStorage.getItem(DISPLAY_SCALE_KEY)).toBe("large");
    expect(loadDisplayScale()).toBe("large");
    expect(document.documentElement.style.getPropertyValue("--scale")).toBe("1.15");
  });
});
