import { useEffect, useState } from "react";
import {
  DISPLAY_SCALES,
  DisplayScaleName,
  loadDisplayScale,
  saveDisplayScale,
} from "../presentation/displayScale";

const OPTIONS: Array<{ id: DisplayScaleName; label: string }> = [
  { id: "compact", label: "A-" },
  { id: "standard", label: "A" },
  { id: "large", label: "A+" },
];

export function DisplayScale() {
  const [scale, setScale] = useState<DisplayScaleName>("standard");

  useEffect(() => {
    const initial = loadDisplayScale();
    setScale(initial);
    saveDisplayScale(initial);
  }, []);

  return (
    <div className="scale-control" role="group" aria-label="Text size">
      {OPTIONS.map((option) => (
        <button
          key={option.id}
          type="button"
          className={scale === option.id ? "active" : ""}
          aria-pressed={scale === option.id}
          title={`Text size ${option.id} (${DISPLAY_SCALES[option.id]})`}
          onClick={() => {
            setScale(option.id);
            saveDisplayScale(option.id);
          }}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
