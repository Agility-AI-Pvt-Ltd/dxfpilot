"use client";

import { useEffect, useState } from "react";
import { api, type BasisApproval, type BasisCandidate, type BasisParameter, type DesignBasis } from "@/lib/api";

type Props = {
  projectId: string;
  /** set when a draft is waiting for these decisions */
  pending?: boolean;
  onClose: () => void;
  /** called after the basis was approved (the waiting draft continues) */
  onApproved: () => void;
  /** draft anyway on the mass balance / cost estimate, flagged as provisional */
  onProvisional?: () => void;
};

const STATUS: Record<BasisParameter["status"], { cls: string; label: string }> = {
  conflict: { cls: "err", label: "Conflict — your decision" },
  approved: { cls: "ok", label: "Approved" },
  agreed: { cls: "ok", label: "Sources agree" },
  single_source: { cls: "plain", label: "One source" },
  missing: { cls: "plain", label: "Not in the workbooks" },
};

function refLabel(c: { ref: string | null }) {
  if (!c.ref) return "";
  const [left, cell] = c.ref.split("!");
  const [file, sheet] = [left.slice(0, left.lastIndexOf(":")), left.slice(left.lastIndexOf(":") + 1)];
  return `${file} › ${sheet}${cell ? ` › row ${cell.replace("R", "")}` : ""}`;
}

type Choice = { pick: number | "other"; other: string; note: string };

export default function BasisPanel({ projectId, pending, onClose, onApproved, onProvisional }: Props) {
  const [basis, setBasis] = useState<DesignBasis | null>(null);
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.basis(projectId).then(setBasis).catch((e) => setError(String(e)));
  }, [projectId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const params = basis?.parameters.filter((p) => p.candidates.length) ?? [];
  const open = params.filter((p) => p.status === "conflict");
  const choice = (id: string): Choice => choices[id] ?? { pick: 0, other: "", note: "" };
  const set = (id: string, c: Partial<Choice>) => setChoices((x) => ({ ...x, [id]: { ...choice(id), ...c } }));

  const approve = async (ids: string[]) => {
    setBusy(true);
    setError(null);
    const approvals: Record<string, BasisApproval> = {};
    for (const id of ids) {
      const p = params.find((q) => q.id === id)!;
      const c = choice(id);
      if (c.pick === "other") {
        const v = Number(c.other.replace(/,/g, ""));
        if (!Number.isFinite(v) || v <= 0) {
          setError(`${p.name}: enter a positive number in ${p.unit}`);
          setBusy(false);
          return;
        }
        approvals[id] = { value: v, source: "Entered by reviewer", ref: null, note: c.note, by: "reviewer" };
      } else {
        const cand: BasisCandidate = p.candidates[c.pick];
        approvals[id] = { value: cand.value, source: cand.source, ref: cand.ref, note: c.note, by: "reviewer" };
      }
    }
    try {
      setBasis(await api.approveBasis(projectId, approvals));
      onApproved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={() => !busy && onClose()}>
      <div className="modal wide" role="dialog" aria-label="Design basis" onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <h2>Design basis</h2>
          <span className="spacer" />
          <button className="btn small" onClick={onClose} aria-label="Close" disabled={busy}>✕</button>
        </div>
        <p style={{ margin: 0, color: "var(--ink-2)", fontSize: 13.5 }}>
          {pending && open.length
            ? `The workbooks disagree on ${open.length} number(s) the drawing depends on. CadPilot does not pick one silently — choose the value to design with. The drafting continues when you approve.`
            : "The numbers the engineering solver designs with, the value each workbook gives, and who approved them."}
        </p>
        {error && <div className="error-banner">{error}</div>}
        {!basis ? (
          <div className="module-hint">Reading the workbooks…</div>
        ) : (
          <div style={{ display: "grid", gap: 10 }}>
            {params
              .sort((a, b) => Number(b.status === "conflict") - Number(a.status === "conflict"))
              .map((p) => {
                const st = STATUS[p.status];
                const c = choice(p.id);
                return (
                  <div key={p.id} className={`basis-param${p.status === "conflict" ? " conflict" : ""}`}>
                    <h4>
                      {p.name}
                      <span className={`badge ${st.cls}`}>{st.label}</span>
                    </h4>
                    <div className="module-hint">Drives: {p.effect}</div>
                    {p.status === "conflict" ? (
                      <>
                        {p.candidates.map((cand, k) => (
                          <label key={k} className="basis-option">
                            <input type="radio" name={p.id} checked={c.pick === k} onChange={() => set(p.id, { pick: k })} disabled={busy} />
                            <span><b>{cand.display}</b> — {cand.source}</span>
                            <span className="src">{refLabel(cand)}{cand.text ? ` — “${cand.text.slice(0, 110)}”` : ""}</span>
                          </label>
                        ))}
                        <label className="basis-option">
                          <input type="radio" name={p.id} checked={c.pick === "other"} onChange={() => set(p.id, { pick: "other" })} disabled={busy} />
                          <span>
                            Other value{" "}
                            <input
                              value={c.other}
                              onChange={(e) => set(p.id, { pick: "other", other: e.target.value })}
                              placeholder={p.unit}
                              style={{ width: 130, marginLeft: 6 }}
                              disabled={busy}
                              aria-label={`Other value for ${p.name} in ${p.unit}`}
                            />{" "}
                            {p.unit}
                          </span>
                        </label>
                        <input
                          value={c.note}
                          onChange={(e) => set(p.id, { note: e.target.value })}
                          placeholder="Reason (optional, recorded with the approval) — e.g. phase 1 capacity per client meeting"
                          disabled={busy}
                          aria-label={`Reason for ${p.name}`}
                        />
                      </>
                    ) : (
                      <div style={{ fontSize: 13 }}>
                        <b>{p.value != null ? `${p.value.toLocaleString()} ${p.unit}` : "—"}</b>
                        {p.approval && (
                          <span style={{ color: "var(--muted)" }}>
                            {" "}— approved by {p.approval.by} ({p.approval.source}{p.approval.note ? `: ${p.approval.note}` : ""})
                          </span>
                        )}
                        <div className="src module-hint">
                          {p.candidates.map((cand) => `${cand.source}: ${cand.display}`).join(" · ")}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
          </div>
        )}
        <div className="modal-actions">
          {pending && onProvisional && open.length > 0 && (
            <button className="btn" style={{ marginRight: "auto" }} onClick={onProvisional} disabled={busy}
                    title="Draft on the mass balance / cost estimate now; the draft is flagged provisional and cannot be issued">
              Draft provisionally
            </button>
          )}
          <button className="btn" onClick={onClose} disabled={busy}>Close</button>
          {open.length > 0 && (
            <button className="btn primary" onClick={() => approve(open.map((p) => p.id))} disabled={busy}>
              {busy ? "Saving…" : pending ? "Approve basis and draft" : "Approve basis"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
