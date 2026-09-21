"use client";

import { Fragment } from "react";
import type { Provenance, Revision } from "@/lib/api";
import DerivedFrom from "./DerivedFrom";

type Props = {
  projectId: string;
  rev: Revision;
  tag: string;
  onSelect: (tag: string) => void;
  onClose: () => void;
  /** send=true posts immediately; false pre-fills the composer for the reviewer to confirm */
  onAsk: (text: string, send: boolean) => void;
};

function fmtQ(q: { value: number; unit: string } | null | undefined) {
  return q ? `${q.value} ${q.unit}` : "—";
}

function Why({ p }: { p: Provenance }) {
  return (
    <div>
      <p className="section-title">Why it exists</p>
      <div className="why">
        {p.rationale || "No rationale recorded."}
        <div style={{ marginTop: 8, color: "var(--muted)", fontSize: 12 }}>
          {p.rule && <>Rule <code>{p.rule}</code> · </>}
          Proposed by {p.agent.replace(/_/g, " ")}
          {p.source && (
            <>
              <br />
              Source: <code>{p.source}</code>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function Tags({ tags, onSelect }: { tags: string[]; onSelect: (t: string) => void }) {
  if (!tags.length) return <span style={{ color: "var(--muted)" }}>—</span>;
  return (
    <div className="taglist">
      {tags.map((t) => (
        <button key={t} className="tagbtn" onClick={() => onSelect(t)}>
          {t}
        </button>
      ))}
    </div>
  );
}

export default function Inspector({ projectId, rev, tag, onSelect, onClose, onAsk }: Props) {
  const m = rev.model;
  const eq = m.equipment.find((e) => e.tag === tag);
  const line = m.lines.find((l) => l.tag === tag);
  const inst = m.instruments.find((i) => i.tag === tag);
  const node = m.piping_nodes.find((n) => n.tag === tag);
  const inlineHost = m.lines.find((l) => l.inline.some((c) => c.tag === tag));
  const inline = inlineHost?.inline.find((c) => c.tag === tag);
  const issues = rev.validation.issues.filter((i) => i.refs.includes(tag));
  const changed = rev.changed_tags.includes(tag);

  let kind = "item";
  let body: React.ReactNode = null;
  let prov: Provenance | undefined;

  if (eq) {
    kind = "equipment";
    prov = eq.provenance;
    const ins = m.lines.filter((l) => l.to.item === tag).map((l) => l.tag);
    const outs = m.lines.filter((l) => l.from.item === tag).map((l) => l.tag);
    const attached = m.instruments.filter((i) => i.attached_to.ref === tag).map((i) => i.tag);
    body = (
      <>
        <dl className="kv">
          <dt>Name</dt><dd>{eq.name}</dd>
          <dt>Type</dt><dd>{eq.type.replace(/_/g, " ")}</dd>
          <dt>Capacity</dt><dd>{fmtQ(eq.capacity)}</dd>
          <dt>Stage</dt><dd>{eq.stage.replace(/_/g, " ")}{eq.train ? ` · train ${eq.train}` : ""}</dd>
          <dt>Area</dt><dd>{eq.area}</dd>
          {Object.entries(eq.attributes).map(([k, v]) => (
            <Fragment key={k}><dt>{k}</dt><dd>{String(v)}</dd></Fragment>
          ))}
        </dl>
        <div><p className="section-title">Inlet lines</p><Tags tags={ins} onSelect={onSelect} /></div>
        <div><p className="section-title">Outlet lines</p><Tags tags={outs} onSelect={onSelect} /></div>
        <div><p className="section-title">Instruments</p><Tags tags={attached} onSelect={onSelect} /></div>
      </>
    );
  } else if (line) {
    kind = "line";
    prov = line.provenance;
    body = (
      <>
        <dl className="kv">
          <dt>From</dt><dd><button className="tagbtn" onClick={() => onSelect(line.from.item)}>{line.from.item}</button> <span style={{ color: "var(--muted)" }}>.{line.from.port}</span></dd>
          <dt>To</dt><dd><button className="tagbtn" onClick={() => onSelect(line.to.item)}>{line.to.item}</button> <span style={{ color: "var(--muted)" }}>.{line.to.port}</span></dd>
          <dt>Service</dt><dd>{line.service}{line.kind !== "process" ? ` · ${line.kind}` : ""}</dd>
          <dt>Design flow</dt><dd>{fmtQ(line.design_flow)}</dd>
          <dt>Size</dt><dd>{line.size_dn ? `DN${line.size_dn}` : "—"}</dd>
          <dt>Spec</dt><dd>{line.spec}</dd>
        </dl>
        <div><p className="section-title">Inline components</p><Tags tags={line.inline.map((c) => c.tag)} onSelect={onSelect} /></div>
        <div><p className="section-title">Instruments</p><Tags tags={m.instruments.filter((i) => i.attached_to.ref === tag).map((i) => i.tag)} onSelect={onSelect} /></div>
      </>
    );
  } else if (inst) {
    kind = "instrument";
    prov = inst.provenance;
    const loop = m.loops.find((l) => l.tag === inst.loop);
    body = (
      <>
        <dl className="kv">
          <dt>Function</dt><dd>{inst.function}</dd>
          <dt>Location</dt><dd>{inst.location}</dd>
          <dt>Attached to</dt><dd><button className="tagbtn" onClick={() => onSelect(inst.attached_to.ref)}>{inst.attached_to.ref}</button></dd>
          {inst.alarms.length > 0 && (<><dt>Alarms</dt><dd>{inst.alarms.join(", ")}</dd></>)}
        </dl>
        {loop && (
          <div>
            <p className="section-title">Control loop {loop.tag}</p>
            <div className="why">
              <Tags tags={[loop.measured_by, loop.tag, loop.final_element].filter((t, i, a) => a.indexOf(t) === i)} onSelect={onSelect} />
              <div style={{ marginTop: 6, fontSize: 12.5, color: "var(--ink-2)" }}>
                Final element: {loop.final_element_kind === "vfd" ? "pump speed (VFD)" : "valve"} · setpoint {loop.setpoint || "—"}
              </div>
            </div>
          </div>
        )}
      </>
    );
  } else if (inline && inlineHost) {
    kind = "valve / inline";
    prov = inline.provenance;
    body = (
      <dl className="kv">
        <dt>Type</dt><dd>{inline.type.replace(/_/g, " ")}</dd>
        <dt>On line</dt><dd><button className="tagbtn" onClick={() => onSelect(inlineHost.tag)}>{inlineHost.tag}</button></dd>
      </dl>
    );
  } else if (node) {
    kind = node.kind.replace("_", " ");
    prov = node.provenance;
    body = (
      <dl className="kv">
        <dt>Kind</dt><dd>{node.kind.replace("_", " ")}</dd>
        {node.label && (<><dt>Label</dt><dd>{node.label}</dd></>)}
      </dl>
    );
  }

  return (
    <aside className="inspector" aria-label={`Inspector for ${tag}`}>
      <header>
        <h3>{tag}</h3>
        <span className="badge plain">{kind}</span>
        {changed && <span className="badge err">changed in {rev.revision}</span>}
        <span className="spacer" />
        <button className="btn small" onClick={onClose} aria-label="Close inspector">✕</button>
      </header>
      <div className="body">
        {body ?? <p className="empty">Not found in this revision.</p>}
        {body && <DerivedFrom projectId={projectId} revision={rev.revision} tag={tag} />}
        {prov && <Why p={prov} />}
        {issues.length > 0 && (
          <div>
            <p className="section-title">Validation</p>
            {issues.map((i, k) => (
              <div key={k} style={{ fontSize: 13, marginBottom: 6 }}>
                <span className={`sev ${i.severity}`}>{i.severity}</span> {i.message}
              </div>
            ))}
          </div>
        )}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button className="btn small" onClick={() => onAsk(`Why is ${tag} required?`, true)}>Ask why</button>
          {eq && <button className="btn small" onClick={() => onAsk(`Add a bypass around ${tag}`, false)}>Add bypass</button>}
          {inst && <button className="btn small" onClick={() => onAsk(`Remove ${tag}`, false)}>Remove…</button>}
        </div>
      </div>
    </aside>
  );
}
