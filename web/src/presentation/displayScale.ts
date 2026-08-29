export const DISPLAY_SCALES = {
  compact: 0.9,
  standard: 1,
  large: 1.15,
} as const;

export type DisplayScaleName = keyof typeof DISPLAY_SCALES;

export const DISPLAY_SCALE_KEY = "projectos.displayScale";

export function parseDisplayScale(value: string | null): DisplayScaleName {
  if (value === "compact" || value === "large") {
    return value;
  }
  return "standard";
}

export function applyDisplayScale(name: DisplayScaleName): void {
  document.documentElement.style.setProperty("--scale", String(DISPLAY_SCALES[name]));
  document.documentElement.dataset.scale = name;
}

export function loadDisplayScale(): DisplayScaleName {
  if (typeof window === "undefined") {
    return "standard";
  }
  return parseDisplayScale(window.localStorage.getItem(DISPLAY_SCALE_KEY));
}

export function saveDisplayScale(name: DisplayScaleName): void {
  window.localStorage.setItem(DISPLAY_SCALE_KEY, name);
  applyDisplayScale(name);
}
