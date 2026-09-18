"use client";

import { useEffect, useState } from "react";
import { api, type ProjectListItem, type RevisionSummary } from "@/lib/api";

type Props = {
  projectId: string;
  current: string | null; // latest revision of the open draft
  viewing: string | null; // revision shown on the right
  refreshKey: string; // changes whenever a revision is added/approved
  busy: boolean;
  onSelectRevision: (rev: string) => void;
  onOpenProject: (id: string) => void;
  onNewDraft: () => void;
};

function when(iso: string) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const today = new Date().toDateString() === d.toDateString();
  return today
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { day: "numeric", month: "short" });
}

const STATUS: Record<RevisionSummary["status"], [string, string]> = {
  ready_for_review: ["ok", "for review"],
  needs_attention: ["err", "needs attention"],
  approved: ["ok", "approved"],
  superseded: ["plain", "superseded"],
};

export default function Sidebar({ projectId, current, viewing, refreshKey, busy, onSelectRevision, onOpenProject, onNewDraft }: Props) {
  const [projects, setProjects] = useState<ProjectListItem[]>([]);
  const [revisions, setRevisions] = useState<RevisionSummary[]>([]);

  useEffect(() => {
    api.projects().then(setProjects).catch(() => {});
  }, [refreshKey]);

  useEffect(() => {
    api.revisions(projectId).then(setRevisions).catch(() => setRevisions([]));
  }, [projectId, refreshKey]);

  return (
    <nav className="sidebar" aria-label="Drafts and versions">
      <div className="side-head">
        <button className="btn primary side-new" onClick={onNewDraft} disabled={busy}>
          + New draft
        </button>
      </div>

      <div className="side-scroll">
        <p className="side-title">Versions of this draft</p>
        {revisions.length === 0 ? (
          <p className="side-empty">{busy ? "Drafting revision A…" : "No versions yet."}</p>
        ) : (
          <ol className="versions">
            {[...revisions].reverse().map((r) => {
              const [tone, label] = STATUS[r.status];
              const isCurrent = r.revision === current;
              return (
                <li key={r.revision}>
                  <button
                    className={`version${r.revision === viewing ? " active" : ""}`}
                    onClick={() => onSelectRevision(r.revision)}
                    aria-current={r.revision === viewing ? "true" : undefined}
                    title={r.diff_summary}
                  >
                    <span className="v-letter">{r.revision}</span>
                    <span className="v-body">
                      <span className="v-row">
                        {isCurrent && <span className="v-current">current</span>}
                        <span className={`v-status ${tone}`}>{label}</span>
                        <span className="v-time">{when(r.created_at)}</span>
                      </span>
                      <span className="v-cause">{r.cause}</span>
                      <span className="v-meta">
                        {r.equipment} eq · {r.instruments} instr
                        {r.errors ? ` · ${r.errors} err` : r.warnings ? ` · ${r.warnings} warn` : ""}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ol>
        )}

        <p className="side-title">All drafts</p>
        <ul className="drafts">
          {projects.map((p) => (
            <li key={p.id}>
              <button
                className={`draft${p.id === projectId ? " active" : ""}`}
                onClick={() => p.id !== projectId && onOpenProject(p.id)}
                disabled={busy && p.id !== projectId}
              >
                <span className="d-name">{p.name}</span>
                <span className="d-meta">
                  {p.drawing_number} · {p.current ? `rev ${p.current}` : "no version"} · {when(p.created_at)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </nav>
  );
}
