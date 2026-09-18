"use client";

import { useState } from "react";
import { api, missingSources, type Project, type SourceRole } from "@/lib/api";
import SourceSlots from "./SourceSlots";

type Props = {
  project: Project;
  onClose: () => void;
  onUploaded: (p: Project) => void;
  /** Regenerate the draft from the (new) data */
  onRegenerate: () => void;
};

export default function DataPanel({ project, onClose, onUploaded, onRegenerate }: Props) {
  const [files, setFiles] = useState<Partial<Record<SourceRole, File>>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string[] | null>(null);
  const s = project.source_summary;
  const intake = s?.mass_balance_inputs.reduce((a, e) => a + (e.litres ?? 0), 0) ?? 0;
  const raw = s?.mass_balance_inputs[0];
  const missing = missingSources(s);
  const chosen = Object.keys(files).length > 0;

  const upload = async (regenerate: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const p = await api.upload(project.id, files);
      onUploaded(p);
      setFiles({});
      const miss = missingSources(p.source_summary);
      if (miss.length) {
        setError([...miss, ...(p.source_summary?.warnings ?? [])]);
      } else if (regenerate) {
        onRegenerate();
        onClose();
      }
    } catch (e) {
      setError([e instanceof Error ? e.message : String(e)]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" role="dialog" aria-label="Design data" onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <h2>Design data</h2>
          <span className="spacer" />
          <button className="btn small" onClick={onClose} aria-label="Close">✕</button>
        </div>

        {s && !missing.length && (
          <div className="facts">
            <div className="card"><div className="n">{intake ? `${(intake / 1000).toLocaleString()} KL` : "—"}</div><div className="l">milk intake / day</div></div>
            <div className="card"><div className="n">{raw?.fat != null ? `${(raw.fat * 100).toFixed(1)} %` : "—"}</div><div className="l">raw milk fat</div></div>
            <div className="card"><div className="n">{raw?.snf != null ? `${(raw.snf * 100).toFixed(1)} %` : "—"}</div><div className="l">raw milk SNF</div></div>
            <div className="card"><div className="n">{s.equipment_rows}</div><div className="l">equipment rows</div></div>
          </div>
        )}
        {missing.length > 0 && !error && (
          <div className="error-banner">{missing.map((m, k) => <div key={k}>{m}</div>)}</div>
        )}
        {error && <div className="error-banner">{error.map((m, k) => <div key={k}>{m}</div>)}</div>}

        <SourceSlots files={files} current={project.sources} onChange={(r, f) => setFiles((x) => ({ ...x, [r]: f }))} disabled={busy} />

        {s?.tables.length ? (
          <p style={{ margin: 0, color: "var(--muted)", fontSize: 12.5 }}>Tables found: {s.tables.join(" · ")}</p>
        ) : null}

        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Close</button>
          {chosen ? (
            <>
              <button className="btn" disabled={busy} onClick={() => upload(false)}>Upload only</button>
              <button className="btn primary" disabled={busy} onClick={() => upload(true)}>
                {busy ? "Reading…" : project.current ? "Upload and regenerate" : "Upload and generate"}
              </button>
            </>
          ) : (
            <button className="btn primary" disabled={busy || missing.length > 0} onClick={() => { onRegenerate(); onClose(); }}>
              {project.current ? "Regenerate from this data" : "Generate draft"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
