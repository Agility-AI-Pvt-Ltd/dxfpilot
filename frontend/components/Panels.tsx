"use client";

import { useEffect, useState } from "react";
import { api, type CategoryDiff, type LLMCall, type Revision } from "@/lib/api";

type SelectFn = (tag: string) => void;

export function ValidationPanel({ rev, onSelect }: { rev: Revision; onSelect: SelectFn }) {
  const v = rev.validation;
  const order = { error: 0, warning: 1, info: 2 } as const;
  const issues = [...v.issues].sort((a, b) => order[a.severity] - order[b.severity]);
  const checks = Object.entries(v.checks_run);
  return (
    <div className="pane">
      <h2>Validation — revision {rev.revision}</h2>
      <p className="sub">Deterministic checks run before the draft reached you. Every issue names the agent that owns the fix.</p>
      <div className="cards">
        <div className="card"><div className="n" style={{ color: v.summary.passed ? "var(--ok)" : "var(--err)" }}>{v.summary.passed ? "PASS" : "FAIL"}</div><div className="l">overall</div></div>
        <div className="card"><div className="n">{v.summary.errors}</div><div className="l">errors</div></div>
        <div className="card"><div className="n">{v.summary.warnings}</div><div className="l">warnings</div></div>
        {checks.map(([layer, n]) => (
          <div className="card" key={layer}><div className="n">{n}</div><div className="l">{layer} checks</div></div>
        ))}
      </div>
      {issues.length === 0 ? (
        <p className="empty">No findings. All structural, topology, engineering-rule and data checks passed.</p>
      ) : (
        <table className="grid">
          <thead><tr><th>Severity</th><th>Layer</th><th>Finding</th><th>Items</th><th>Rule</th></tr></thead>
          <tbody>
            {issues.map((i, k) => (
              <tr key={k}>
                <td><span className={`sev ${i.severity}`}>{i.severity}</span></td>
                <td>{i.layer}</td>
                <td>{i.message}</td>
                <td>
                  <div className="taglist">
                    {i.refs.slice(0, 4).map((t) => <button key={t} className="tagbtn" onClick={() => onSelect(t)}>{t}</button>)}
                  </div>
                </td>
                <td><code>{i.rule ?? i.code}</code></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function DiffRows({ name, d, onSelect }: { name: string; d: CategoryDiff; onSelect: SelectFn }) {
  const rows: [string, string, string][] = [
    ...d.added.map((t) => [t, "added", ""] as [string, string, string]),
    ...d.removed.map((t) => [t, "removed", ""] as [string, string, string]),
    ...Object.entries(d.modified).map(([t, f]) => [t, "modified", f.join(", ")] as [string, string, string]),
  ];
  if (!rows.length) return null;
  return (
    <>
      {rows.map(([t, kind, fields]) => (
        <tr key={`${name}${t}`} className={kind !== "removed" ? "clickable" : ""} onClick={() => kind !== "removed" && onSelect(t)}>
          <td>{name}</td>
          <td><code className={`diff-${kind}`}>{t}</code></td>
          <td className={`diff-${kind}`}>{kind}</td>
          <td>{fields}</td>
        </tr>
      ))}
    </>
  );
}

export function ChangesPanel({ rev, onSelect }: { rev: Revision; onSelect: SelectFn }) {
  const cr = rev.change_request;
  return (
    <div className="pane">
      <h2>Changes in revision {rev.revision}</h2>
      <p className="sub">{rev.diff_summary}</p>
      {cr && (
        <div className="card" style={{ marginBottom: 16 }}>
          <p className="section-title">Reviewer correction</p>
          <p style={{ margin: "0 0 8px" }}>“{cr.message}”</p>
          <p className="section-title">Interpreted as structured change ({cr.interpreter})</p>
          <pre className="mono" style={{ margin: 0, whiteSpace: "pre-wrap" }}>{JSON.stringify(cr.operations, null, 2)}</pre>
        </div>
      )}
      {rev.diff ? (
        <table className="grid">
          <thead><tr><th>Category</th><th>Tag</th><th>Change</th><th>Fields</th></tr></thead>
          <tbody>
            <DiffRows name="equipment" d={rev.diff.equipment} onSelect={onSelect} />
            <DiffRows name="line" d={rev.diff.lines} onSelect={onSelect} />
            <DiffRows name="instrument" d={rev.diff.instruments} onSelect={onSelect} />
            <DiffRows name="valve" d={rev.diff.valves} onSelect={onSelect} />
          </tbody>
        </table>
      ) : (
        <p className="empty">Initial draft — nothing to compare against.</p>
      )}
    </div>
  );
}

export function ListsPanel({ rev, onSelect }: { rev: Revision; onSelect: SelectFn }) {
  const m = rev.model;
  return (
    <div className="pane">
      <h2>Equipment, line and instrument lists</h2>
      <p className="sub">Generated from the same engineering model as the drawing — they cannot disagree.</p>
      <p className="section-title">Equipment ({m.equipment.length})</p>
      <table className="grid" style={{ marginBottom: 20 }}>
        <thead><tr><th>Tag</th><th>Description</th><th>Capacity</th><th>Stage</th><th>Source</th></tr></thead>
        <tbody>
          {m.equipment.map((e) => (
            <tr key={e.tag} className="clickable" onClick={() => onSelect(e.tag)}>
              <td><code>{e.tag}</code></td><td>{e.name}</td>
              <td>{e.capacity ? `${e.capacity.value} ${e.capacity.unit}` : "—"}</td>
              <td>{e.stage.replace(/_/g, " ")}</td>
              <td style={{ color: "var(--muted)", fontSize: 12 }}>{e.provenance.source?.split(":").slice(-1)[0]}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="section-title">Lines ({m.lines.length})</p>
      <table className="grid" style={{ marginBottom: 20 }}>
        <thead><tr><th>Line</th><th>From → To</th><th>Service</th><th>Flow</th><th>Size</th><th>Inline</th></tr></thead>
        <tbody>
          {m.lines.map((l) => (
            <tr key={l.tag} className="clickable" onClick={() => onSelect(l.tag)}>
              <td><code>{l.tag}</code></td><td>{l.from.item} → {l.to.item}</td><td>{l.service}{l.kind === "bypass" ? " (bypass)" : ""}</td>
              <td>{l.design_flow ? `${l.design_flow.value} ${l.design_flow.unit}` : "—"}</td>
              <td>{l.size_dn ? `DN${l.size_dn}` : "—"}</td>
              <td style={{ fontSize: 12 }}>{l.inline.map((c) => c.tag).join(", ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="section-title">Instruments ({m.instruments.length})</p>
      <table className="grid">
        <thead><tr><th>Tag</th><th>Function</th><th>On</th><th>Loop</th><th>Rule</th></tr></thead>
        <tbody>
          {m.instruments.map((i) => (
            <tr key={i.tag} className="clickable" onClick={() => onSelect(i.tag)}>
              <td><code>{i.tag}</code></td><td>{i.function}{i.alarms.length ? ` (${i.alarms.join(", ")})` : ""}</td>
              <td>{i.attached_to.ref}</td><td>{i.loop ?? "—"}</td><td><code>{i.provenance.rule}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LLMCallsTable({ projectId, refreshKey }: { projectId: string; refreshKey: string }) {
  const [calls, setCalls] = useState<LLMCall[] | null>(null);
  useEffect(() => {
    api.llmCalls(projectId).then(setCalls).catch(() => setCalls([]));
  }, [projectId, refreshKey]);
  if (calls === null) return <p className="empty">Loading…</p>;
  if (!calls.length) return <p className="empty">No LLM calls for this project — every answer came from the rule-based agents.</p>;
  return (
    <table className="grid" style={{ marginBottom: 20 }}>
      <thead><tr><th>When</th><th>Purpose</th><th>Outcome</th><th>Model (served)</th><th>Time</th><th>Tokens in / out</th><th>Rev</th></tr></thead>
      <tbody>
        {[...calls].reverse().map((c, k) => (
          <tr key={k} title={c.detail}>
            <td style={{ whiteSpace: "nowrap" }}>{c.at.replace("T", " ").slice(0, 19)}</td>
            <td><code>{c.purpose}</code></td>
            <td><span className={`sev ${c.outcome === "ok" ? "info" : "warning"}`}>{c.outcome}</span>{c.detail && c.outcome !== "ok" ? <div style={{ fontSize: 12, color: "var(--muted)" }}>{c.detail}</div> : null}</td>
            <td><code>{c.served_model ?? "—"}</code>{c.served_model && c.served_model !== c.model ? <div style={{ fontSize: 12, color: "var(--muted)" }}>requested {c.model}</div> : null}</td>
            <td>{(c.latency_ms / 1000).toFixed(1)} s</td>
            <td>{c.prompt_tokens ?? "—"} / {c.completion_tokens ?? "—"}</td>
            <td>{c.revision ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function DecisionsPanel({ rev, projectId }: { rev: Revision; projectId: string }) {
  const md = rev.model.metadata;
  return (
    <div className="pane">
      <h2>Agent decisions</h2>
      <p className="sub">What the primary agent decided, and what the workers assumed where the data was silent.</p>
      <p className="section-title">Engine</p>
      <p style={{ margin: "0 0 12px" }}>
        {md.engine === "llm_planner" ? "LLM planner (experimental) — plan validated deterministically" : "Rules engine"}
      </p>
      {md.planner && (
        <>
          <p className="section-title">LLM planner attempts ({md.planner.attempts} of max {md.planner.max_attempts})</p>
          {md.planner.stop_reason && (
            <p style={{ margin: "0 0 8px", fontSize: 13 }}>
              Stopped: {md.planner.stop_reason}
              {md.planner.best_attempt ? ` — this drawing is attempt ${md.planner.best_attempt}, the one with the fewest errors.` : ""}
            </p>
          )}
          <table className="grid" style={{ marginBottom: 20 }}>
            <thead><tr><th>Attempt</th><th>Plan</th><th>Validation</th></tr></thead>
            <tbody>
              {md.planner.history.map((h) => (
                <tr key={h.attempt} style={h.kept === false ? { opacity: 0.6 } : undefined}>
                  <td>
                    {h.attempt}
                    {h.attempt === md.planner!.best_attempt && <div><span className="badge ok">used</span></div>}
                    {h.kept === false && <div style={{ fontSize: 11.5, color: "var(--muted)" }}>discarded (worse)</div>}
                  </td>
                  <td style={{ fontSize: 12.5 }}>
                    {h.plan}
                    {h.changes && h.attempt > 1 && <div style={{ color: "var(--muted)" }}>patch: {h.changes}</div>}
                    {h.patch_problems?.map((x, k) => <div key={k} className="sev warning" style={{ textTransform: "none" }}>{x}</div>)}
                  </td>
                  <td style={{ fontSize: 12.5 }}>
                    {h.errors.length === 0 ? <span className="sev info">passed</span> : (
                      <details>
                        <summary className="sev error" style={{ cursor: "pointer" }}>{h.errors.length} error(s)</summary>
                        {h.errors.map((e, k) => <div key={k}>{e}</div>)}
                      </details>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <p className="section-title">LLM calls (audit log, newest first)</p>
      <LLMCallsTable projectId={projectId} refreshKey={rev.revision} />
      <p className="section-title">Run log</p>
      <table className="grid" style={{ marginBottom: 20 }}>
        <tbody>
          {rev.events.map((e, k) => (
            <tr key={k}><td style={{ width: 170 }}><code>{e.step}</code></td><td>{e.detail}{e.llm ? " · LLM" : ""}</td></tr>
          ))}
        </tbody>
      </table>
      <p className="section-title">Decisions</p>
      {md.decisions?.length ? (
        <table className="grid" style={{ marginBottom: 20 }}>
          <tbody>
            {md.decisions.map((d, k) => (
              <tr key={k}><td style={{ width: 280 }}><b>{d.summary}</b></td><td>{d.detail}</td></tr>
            ))}
          </tbody>
        </table>
      ) : <p className="empty">None recorded.</p>}
      <p className="section-title">Assumptions</p>
      {md.assumptions?.length ? <ul>{md.assumptions.map((a, k) => <li key={k}>{a}</li>)}</ul> : <p className="empty">None — every value came from the design data.</p>}
      {md.warnings?.length ? (<><p className="section-title">Worker warnings</p><ul>{md.warnings.map((a, k) => <li key={k}>{a}</li>)}</ul></>) : null}
    </div>
  );
}
