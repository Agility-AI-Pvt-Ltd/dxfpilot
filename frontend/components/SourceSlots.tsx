"use client";

import { useRef, useState } from "react";
import type { SourceRole } from "@/lib/api";

export const SLOTS: { role: SourceRole; title: string; hint: string }[] = [
  { role: "mass_balance", title: "Mass balance", hint: "Milk intake and product outputs (in L, in Kg, Fat, SNF)" },
  { role: "design_data", title: "Equipment & design data", hint: "Design criteria and equipment list (description, capacity, qty)" },
];

type Props = {
  files: Partial<Record<SourceRole, File>>;
  onChange: (role: SourceRole, file: File) => void;
  current?: Partial<Record<SourceRole, string>>; // filenames already on the project
  /** Where `current` came from, when it counts as chosen (e.g. "from the CRM") */
  currentSource?: string;
  disabled?: boolean;
};

function Slot({ role, title, hint, file, current, currentSource, onChange, disabled }: {
  role: SourceRole; title: string; hint: string; file?: File; current?: string; currentSource?: string; onChange: Props["onChange"]; disabled?: boolean;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const take = (f?: File) => {
    if (!f) return;
    if (!f.name.toLowerCase().endsWith(".xlsx")) {
      setError("Only .xlsx workbooks are supported");
      return;
    }
    setError(null);
    onChange(role, f);
  };
  const name = file?.name ?? current;
  const filled = !!file || (!!current && !!currentSource);
  return (
    <div
      className={`slot${filled ? " has" : current ? " current" : ""}${over ? " over" : ""}`}
      role="button"
      tabIndex={0}
      aria-label={`Upload ${title} workbook`}
      onClick={() => !disabled && input.current?.click()}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && !disabled && input.current?.click()}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); if (!disabled) take(e.dataTransfer.files[0]); }}
    >
      <div className="slot-icon" aria-hidden>{filled ? "✓" : "xlsx"}</div>
      <div className="slot-text">
        <b>{title}</b>
        <span>{name ? name : hint}</span>
        {file && current && <span className="slot-note">replaces {current}</span>}
        {!file && current && currentSource && <span className="slot-note">{currentSource}</span>}
        {error && <span className="slot-err">{error}</span>}
      </div>
      <span className="slot-action">{name ? "Replace" : "Choose file"}</span>
      <input ref={input} type="file" accept=".xlsx" hidden onChange={(e) => { take(e.target.files?.[0]); e.target.value = ""; }} />
    </div>
  );
}

export default function SourceSlots({ files, onChange, current, currentSource, disabled }: Props) {
  return (
    <div className="slots">
      {SLOTS.map((s) => (
        <Slot key={s.role} {...s} file={files[s.role]} current={current?.[s.role]} currentSource={currentSource} onChange={onChange} disabled={disabled} />
      ))}
    </div>
  );
}
