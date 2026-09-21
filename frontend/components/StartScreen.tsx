"use client";

import { useEffect, useState } from "react";
import { api, ENGINE_LABEL, missingSources, type Engine, type ProjectListItem, type SourceRole } from "@/lib/api";
import ModulePicker from "./ModulePicker";
import SourceSlots from "./SourceSlots";

type Props = {
  /** modules undefined + detect: the scope agent reads the workbooks and chooses the plant sections */
  onReady: (projectId: string, request: string, engine?: Engine, modules?: string[], detect?: boolean) => void;
  /** Present when there is a draft to go back to: shows ✕ / Cancel and closes on Esc. */
  onCancel?: () => void;
};

export default function StartScreen({ onReady, onCancel }: Props) {
  const [name, setName] = useState(
    () => `Milk reception & processing — ${new Date().toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`,
  );
  const [engine, setEngine] = useState<Engine>("rules");
  const [scopeMode, setScopeMode] = useState<"detect" | "manual">("detect");
  const [modules, setModules] = useState<string[]>(["reception", "pasteurization"]);
  const [request, setRequest] = useState("Generate the P&ID from the design data");
  const [files, setFiles] = useState<Partial<Record<SourceRole, File>>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string[] | null>(null);
  const [recent, setRecent] = useState<ProjectListItem[]>([]);

  useEffect(() => {
    if (!onCancel) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel, busy]);

  useEffect(() => {
    api.projects().then(setRecent).catch(() => setError(["Cannot reach the CadPilot API on :8000 — is the backend running?"]));
  }, []);

  const bothChosen = !!files.mass_balance && !!files.design_data;

  const start = async (demo: boolean) => {
    setBusy(true);
    setError(null);
    try {
      let p = await api.create(name.trim() || "Untitled project", demo);
      if (!demo) p = await api.upload(p.id, files);
      // Check the workbooks actually contain what the agents need before drafting
      const missing = missingSources(p.source_summary);
      if (missing.length) {
        setError([...missing, ...(p.source_summary?.warnings ?? []), "Check that each file is in the right slot and uses the expected table headers."]);
        setBusy(false);
        return;
      }
      if (scopeMode === "detect") onReady(p.id, request, engine, undefined, true);
      else onReady(p.id, request, engine, modules, false);
    } catch (e) {
      setError([e instanceof Error ? e.message : String(e)]);
      setBusy(false);
    }
  };

  return (
    <div className="start">
      <div className="start-card">
        <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
          <div style={{ flex: 1 }}>
          <h1>New P&amp;ID draft</h1>
          <p>Upload the two workbooks. CadPilot drafts the complete, validated P&amp;ID; you review and correct it in conversation.</p>
          </div>
          {onCancel && (
            <button className="btn small" onClick={onCancel} disabled={busy} aria-label="Close and go back to the current draft" title="Close (Esc)">
              ✕
            </button>
          )}
        </div>
        {error && (
          <div className="error-banner">
            {error.map((e, k) => <div key={k}>{e}</div>)}
          </div>
        )}
        <div className="field">
          <label htmlFor="pname">Project name</label>
          <input id="pname" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field">
          <label>Design data — two .xlsx workbooks (click or drag &amp; drop)</label>
          <SourceSlots files={files} onChange={(role, f) => setFiles((x) => ({ ...x, [role]: f }))} disabled={busy} />
        </div>
        <div className="field">
          <label htmlFor="req">What should CadPilot draft?</label>
          <textarea id="req" rows={2} value={request} onChange={(e) => setRequest(e.target.value)} />
        </div>
        <div className="field">
          <label>Plant sections (engineering modules)</label>
          <div className="scope-mode" role="radiogroup" aria-label="How to choose the plant sections">
            <label>
              <input type="radio" name="scope" checked={scopeMode === "detect"} onChange={() => setScopeMode("detect")} disabled={busy} />
              <span>
                <b>Detect from the workbooks</b> (recommended)
                <div className="module-hint">
                  CadPilot reads the equipment list, draws every plant section it finds and tells you which rows it
                  found them in. You can add or remove sections afterwards under Design data.
                </div>
              </span>
            </label>
            <label>
              <input type="radio" name="scope" checked={scopeMode === "manual"} onChange={() => setScopeMode("manual")} disabled={busy} />
              <span>
                <b>Choose the sections myself</b>
              </span>
            </label>
          </div>
          {scopeMode === "manual" && <ModulePicker value={modules} onChange={setModules} disabled={busy} />}
        </div>
        <div className="field">
          <label htmlFor="engine">Draft engine</label>
          <select id="engine" value={engine} onChange={(e) => setEngine(e.target.value as Engine)}
                  style={{ border: "1px solid var(--line-strong)", borderRadius: 8, padding: "9px 12px", background: "var(--panel)" }}>
            {(Object.keys(ENGINE_LABEL) as Engine[]).map((k) => <option key={k} value={k}>{ENGINE_LABEL[k]}</option>)}
          </select>
          {engine === "llm_planner" && (
            <span style={{ fontSize: 12.5, color: "var(--ink-2)" }}>
              The LLM plans connections, valves and instruments; the same validator checks the plan and sends errors back
              (up to 3 attempts). If it still fails, the draft comes to you for review instead of looping.
            </span>
          )}
        </div>
        <button className="btn primary" disabled={busy || !bothChosen} onClick={() => start(false)}>
          {busy ? "Reading workbooks…" : bothChosen ? "Upload and generate draft" : "Choose both workbooks to continue"}
        </button>
        <div className="or">or</div>
        <button className="btn" disabled={busy} onClick={() => start(true)}>Use the bundled dairy dummy data</button>
        {onCancel && (
          <button className="btn" disabled={busy} onClick={onCancel}>Cancel — back to the current draft</button>
        )}
        {recent.length > 0 && (
          <div className="recent">
            <p className="section-title" style={{ marginTop: 6 }}>Recent projects</p>
            {recent.slice(0, 5).map((r) => (
              <button key={r.id} onClick={() => onReady(r.id, "")}>
                <span>{r.name}</span>
                <span className="mono" style={{ color: "var(--muted)" }}>{r.current ? `rev ${r.current}` : "no draft"}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
