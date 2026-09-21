"use client";

import { useState } from "react";
import { api, ENGINE_LABEL, missingSources, type Engine, type Project, type SourceRole, type SourceRow, type TableDetection, type TableKind, type TableOverride } from "@/lib/api";
import MappingEditor from "./MappingEditor";
import ModulePicker from "./ModulePicker";
import SourceSlots from "./SourceSlots";

const KIND = { equipment_list: "Equipment list", mass_balance: "Mass balance", design_criteria: "Design criteria" } as const;
const METHOD = { headers: "headers", llm: "AI-mapped", content: "read from values", manual: "your mapping" } as const;
const METHOD_TONE = { headers: "ok", llm: "info", content: "warn", manual: "plain" } as const;

type Props = {
  project: Project;
  onClose: () => void;
  onUploaded: (p: Project) => void;
  /** Regenerate the draft from the (new) data */
  onRegenerate: (engine: Engine, modules: string[]) => void;
};

/** Starting point for the editor: the saved mapping, else what automatic detection found. */
function initialMapping(project: Project, kind: TableKind, d?: TableDetection): Partial<TableOverride> | null {
  const saved = project.table_overrides.find((o) => o.kind === kind);
  if (saved) return saved;
  if (!d) return null;
  const columns: Record<string, string> = {};
  for (const [field, label] of Object.entries(d.columns)) {
    const letter = label.split(":")[0].trim();
    if (!/^[A-Z]{1,3}$/.test(letter)) continue;
    const key = field.replace(/^input /, "").replace(/^output /, "output_");
    columns[key] = letter;
  }
  return { file: d.file, sheet: d.sheet, header_row: d.header_row, columns, capacity_unit: d.columns["capacity unit"] ?? null };
}

function RowList({ rows, tone }: { rows: SourceRow[]; tone: "warn" | "info" }) {
  return (
    <table className="grid">
      <tbody>
        {rows.map((r) => (
          <tr key={r.ref}>
            <td style={{ width: 70 }}><code>{r.ref.split("!").pop()}</code></td>
            <td>
              {r.description}
              <div style={{ fontSize: 11.5, color: "var(--muted)" }}>
                qty cell <code>{r.qty_raw ?? "—"}</code> · capacity cell <code>{r.capacity_raw ?? "—"}</code>
              </div>
            </td>
            <td style={{ fontSize: 12.5 }}>
              {tone === "warn"
                ? r.flags.map((f, k) => <div key={k} className="sev warning" style={{ textTransform: "none" }}>{f}</div>)
                : <>
                    {r.qty != null && <div>× {r.qty}</div>}
                    {r.capacity && <div>{r.capacity.value} {r.capacity.unit}</div>}
                    <div style={{ color: "var(--muted)" }}>{r.note}</div>
                  </>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function DataPanel({ project, onClose, onUploaded, onRegenerate }: Props) {
  const [files, setFiles] = useState<Partial<Record<SourceRole, File>>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string[] | null>(null);
  const [editing, setEditing] = useState<{ kind: TableKind; initial: Partial<TableOverride> | null } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [engine, setEngine] = useState<Engine>(project.intent?.engine ?? "rules");
  const [modules, setModules] = useState<string[]>(project.modules ?? []);
  const [findings, setFindings] = useState(project.intent?.scope_detection?.modules ?? null);
  const [detecting, setDetecting] = useState(false);
  const detect = async () => {
    setDetecting(true);
    setError(null);
    try {
      const det = await api.detectScope(project.id);
      setFindings(det.modules);
      setModules(det.selected);
      setNotice(`Found ${det.selected.length} plant section(s) in the workbooks — regenerate to draw them.`);
    } catch (e) {
      setError([e instanceof Error ? e.message : String(e)]);
    } finally {
      setDetecting(false);
    }
  };
  const s = project.source_summary;
  const intake = s?.mass_balance_inputs.reduce((a, e) => a + (e.litres ?? 0), 0) ?? 0;
  const raw = s?.mass_balance_inputs[0];
  const missing = missingSources(s);
  const chosen = Object.keys(files).length > 0;
  const detected = new Set(s?.detections?.map((d) => d.kind) ?? []);
  const flagged = s?.flagged_rows ?? [];
  const aiRead = s?.ai_read_rows ?? [];

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
        onRegenerate(engine, modules);
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
      <div className={`modal${editing ? " wide" : ""}`} role="dialog" aria-label="Design data" onClick={(e) => e.stopPropagation()}>
        {editing ? (
          <MappingEditor
            project={project}
            kind={editing.kind}
            initial={editing.initial}
            onCancel={() => setEditing(null)}
            onSaved={(p) => {
              onUploaded(p);
              setEditing(null);
              setNotice(
                project.current
                  ? "Mapping saved and the workbooks re-read. Regenerate to apply it to the drawing."
                  : "Mapping saved and the workbooks re-read.",
              );
            }}
          />
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <h2>Design data</h2>
              <span className="spacer" />
              <button className="btn small" onClick={onClose} aria-label="Close">✕</button>
            </div>

            {notice && <div className="notice">{notice}</div>}

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

            <div>
              <p className="section-title">Plant sections on this drawing</p>
              <ModulePicker value={modules} onChange={setModules} disabled={busy} findings={findings} onDetect={detect} detecting={detecting} />
            </div>

            <SourceSlots files={files} current={project.sources} onChange={(r, f) => setFiles((x) => ({ ...x, [r]: f }))} disabled={busy} />

            {s && (
              <div>
                <p className="section-title">How the workbooks were read</p>
                <table className="grid">
                  <thead><tr><th>Table</th><th>Found by</th><th>Columns</th><th /></tr></thead>
                  <tbody>
                    {s.detections?.map((d, k) => (
                      <tr key={k}>
                        <td>
                          {KIND[d.kind]}
                          <div style={{ fontSize: 11.5, color: "var(--muted)" }}>{d.sheet} · {d.rows} rows{d.header_row ? ` · header row ${d.header_row}` : " · no header row"}</div>
                        </td>
                        <td><span className={`badge ${METHOD_TONE[d.method]}`}>{METHOD[d.method]}</span></td>
                        <td style={{ fontSize: 12 }}>
                          {Object.entries(d.columns).map(([f, c]) => (
                            <div key={f}><span style={{ color: "var(--muted)" }}>{f}</span> ← <code>{c}</code></div>
                          ))}
                        </td>
                        <td>
                          {d.kind !== "design_criteria" && (
                            <button className="btn small" disabled={busy} onClick={() => setEditing({ kind: d.kind as TableKind, initial: initialMapping(project, d.kind as TableKind, d) })}>
                              Change…
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                    {(["equipment_list", "mass_balance"] as TableKind[]).filter((k) => !detected.has(k) && project.sources && Object.keys(project.sources).length).map((k) => (
                      <tr key={k}>
                        <td>{KIND[k]}</td>
                        <td><span className="badge err">not found</span></td>
                        <td style={{ fontSize: 12, color: "var(--muted)" }}>Point CadPilot at the right sheet and columns.</td>
                        <td>
                          <button className="btn small primary" disabled={busy} onClick={() => setEditing({ kind: k, initial: initialMapping(project, k) })}>
                            Map manually…
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {flagged.length > 0 && (
              <details open={flagged.length <= 5}>
                <summary className="section-title" style={{ cursor: "pointer" }}>
                  Rows needing attention ({flagged.length}) — kept, but a value could not be read
                </summary>
                <RowList rows={flagged} tone="warn" />
              </details>
            )}
            {aiRead.length > 0 && (
              <details>
                <summary className="section-title" style={{ cursor: "pointer" }}>Values read by the AI ({aiRead.length})</summary>
                <RowList rows={aiRead} tone="info" />
              </details>
            )}

            <div className="modal-actions">
              <select value={engine} onChange={(e) => setEngine(e.target.value as Engine)} aria-label="Draft engine"
                      style={{ marginRight: "auto", border: "1px solid var(--line-strong)", borderRadius: 6, padding: "5px 8px", background: "var(--panel)" }}>
                {(Object.keys(ENGINE_LABEL) as Engine[]).map((k) => <option key={k} value={k}>{ENGINE_LABEL[k]}</option>)}
              </select>
              <button className="btn" onClick={onClose}>Close</button>
              {chosen ? (
                <>
                  <button className="btn" disabled={busy} onClick={() => upload(false)}>Upload only</button>
                  <button className="btn primary" disabled={busy} onClick={() => upload(true)}>
                    {busy ? "Reading…" : project.current ? "Upload and regenerate" : "Upload and generate"}
                  </button>
                </>
              ) : (
                <button className="btn primary" disabled={busy || missing.length > 0} onClick={() => { onRegenerate(engine, modules); onClose(); }}>
                  {project.current ? "Regenerate from this data" : "Generate draft"}
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
