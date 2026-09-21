"use client";

import { useEffect, useState } from "react";
import { api, type EngineeringModule, type ScopeFinding } from "@/lib/api";

type Props = {
  value: string[];
  onChange: (ids: string[]) => void;
  disabled?: boolean;
  /** What the workbooks contain, per module (shown on each card) */
  findings?: ScopeFinding[] | null;
  /** Re-read the workbooks and select what they contain */
  onDetect?: () => void;
  detecting?: boolean;
};

const STATUS = {
  found: { cls: "ok", label: "In workbooks" },
  partial: { cls: "warn", label: "Partly in workbooks" },
  not_found: { cls: "plain", label: "Not in workbooks" },
} as const;

/** Engineering modules (plant building blocks) to draw. None selected = the classic single line. */
export default function ModulePicker({ value, onChange, disabled, findings, onDetect, detecting }: Props) {
  const [modules, setModules] = useState<EngineeringModule[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    api.modules().then(setModules).catch(() => setError(true));
  }, []);

  const toggle = (id: string) => onChange(value.includes(id) ? value.filter((v) => v !== id) : [...value, id]);
  const finding = (id: string) => findings?.find((f) => f.id === id);

  if (error) return <div className="module-hint">Engineering modules unavailable — the classic line will be drawn.</div>;
  if (!modules) return <div className="module-hint">Loading modules…</div>;

  const process = modules.filter((m) => !m.utility);
  const utilities = modules.filter((m) => m.utility);
  const card = (m: EngineeringModule) => {
    const on = value.includes(m.id);
    const f = finding(m.id);
    const evidence = f?.evidence.map((e) => `${e.ref.split("!").pop()}: ${e.text}${e.capacity ? ` (${e.capacity})` : ""}`) ?? [];
    return (
      <button
        key={m.id}
        type="button"
        className={`module-card${on ? " on" : ""}`}
        onClick={() => toggle(m.id)}
        disabled={disabled}
        aria-pressed={on}
        aria-label={`${m.area}. ${m.name}: ${m.summary}${f ? ` — ${STATUS[f.status].label}` : ""}`}
        title={[m.equipment.join(" → "), `${m.rules} module-specific rule(s)`, ...(evidence.length ? ["", "Found in the workbooks:", ...evidence] : [])].join("\n")}
      >
        <span className="module-check" aria-hidden="true">{on ? "✓" : ""}</span>
        <span className="module-name">
          {m.area}. {m.name}
        </span>
        <span className="module-summary">{m.summary}</span>
        {f && (
          <span className={`module-evidence ${STATUS[f.status].cls}`}>
            {STATUS[f.status].label}
            {f.evidence.length ? ` · ${f.evidence.length} row${f.evidence.length > 1 ? "s" : ""}` : ""}
            {f.found_by === "ai" ? " · AI" : ""}
          </span>
        )}
      </button>
    );
  };

  return (
    <div className="module-picker">
      <div className="module-row-label">Process</div>
      <div className="module-grid">{process.map(card)}</div>
      <div className="module-row-label">Utilities — one supply per consumer in the selected process modules</div>
      <div className="module-grid">{utilities.map(card)}</div>
      <div className="module-actions">
        {onDetect && (
          <button type="button" className="btn small" disabled={disabled || detecting} onClick={onDetect}>
            {detecting ? "Reading workbooks…" : "Detect from workbooks"}
          </button>
        )}
        <button type="button" className="btn small" disabled={disabled} onClick={() => onChange(modules.map((m) => m.id))}>
          Whole plant
        </button>
        <button type="button" className="btn small" disabled={disabled || !value.length} onClick={() => onChange([])}>
          Clear
        </button>
        <span className="module-hint">
          {value.length
            ? `${value.length} module${value.length > 1 ? "s" : ""} — one band per module on the drawing`
            : "None selected: the classic reception → pasteurization line"}
        </span>
      </div>
    </div>
  );
}
