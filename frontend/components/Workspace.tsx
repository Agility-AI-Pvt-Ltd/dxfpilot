"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamPost, type Engine, type Project, type ProgressEvent, type Revision } from "@/lib/api";
import Chat from "./Chat";
import DataPanel from "./DataPanel";
import Inspector from "./Inspector";
import { ChangesPanel, DecisionsPanel, ListsPanel, ValidationPanel } from "./Panels";
import PidViewer from "./PidViewer";
import Sidebar from "./Sidebar";

type Tab = "drawing" | "validation" | "changes" | "lists" | "decisions";

type Props = {
  projectId: string;
  initialRequest: string;
  initialEngine?: Engine;
  initialModules?: string[];
  initialDetect?: boolean;
  onExit: () => void;
  onOpenProject: (id: string) => void;
};

const SIDEBAR_KEY = "cadpilot.sidebar";

export default function Workspace({ projectId, initialRequest, initialEngine, initialModules, initialDetect, onExit, onOpenProject }: Props) {
  const [project, setProject] = useState<Project | null>(null);
  const [revLetter, setRevLetter] = useState<string | null>(null);
  const [rev, setRev] = useState<Revision | null>(null);
  const [svg, setSvg] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("drawing");
  const [live, setLive] = useState<ProgressEvent[] | null>(null);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [llm, setLlm] = useState<boolean | null>(null);
  const [showData, setShowData] = useState(false);
  const [sidebar, setSidebar] = useState(true);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(SIDEBAR_KEY);
      if (saved) setSidebar(saved === "open");
      else if (window.matchMedia("(max-width: 900px)").matches) setSidebar(false);
    } catch {}
  }, []);
  const toggleSidebar = () =>
    setSidebar((open) => {
      try {
        localStorage.setItem(SIDEBAR_KEY, open ? "closed" : "open");
      } catch {}
      return !open;
    });
  const isNarrow = () => typeof window !== "undefined" && window.matchMedia("(max-width: 900px)").matches;
  const started = useRef(false);

  const refresh = useCallback(async (open?: string) => {
    const p = await api.project(projectId);
    setProject(p);
    const letter = open ?? p.current;
    if (letter) setRevLetter(letter);
    return p;
  }, [projectId]);

  useEffect(() => {
    api.health().then((h) => setLlm(h.llm)).catch(() => setLlm(null));
  }, []);

  useEffect(() => {
    if (!revLetter) return;
    let cancelled = false;
    Promise.all([api.revision(projectId, revLetter), api.svg(projectId, revLetter)]).then(([r, s]) => {
      if (cancelled) return;
      setRev(r);
      setSvg(s);
    });
    return () => {
      cancelled = true;
    };
  }, [projectId, revLetter]);

  const run = useCallback(
    async (path: string, body: unknown) => {
      setLive([]);
      setError(null);
      try {
        const result = await streamPost(path, body, (ev) => setLive((l) => [...(l ?? []), ev]));
        await refresh((result.revision as string) || undefined);
        if (result.revision) setTab((t) => (t === "drawing" ? t : "changes"));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        await refresh();
      } finally {
        setLive(null);
      }
    },
    [refresh],
  );

  // First visit: load, and generate immediately if there is no draft yet (autonomous first draft)
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    refresh().then((p) => {
      if (p.current) return;
      if (p.source_summary?.equipment_rows) run(`/api/projects/${projectId}/generate`, {
          request: initialRequest || "Generate the P&ID",
          engine: initialEngine,
          modules: initialModules,
          detect_modules: !!initialDetect,
        });
      else setShowData(true); // no usable data yet: ask for the workbooks
    });
  }, [projectId, initialRequest, initialEngine, initialModules, initialDetect, refresh, run]);

  const send = (text?: string) => {
    const msg = (text ?? input).trim();
    if (!msg || live) return;
    setInput("");
    setProject((p) => p && { ...p, chat: [...p.chat, { role: "user", text: msg, at: "", data: selected ? { selected } : {} }] });
    if (!project?.current) run(`/api/projects/${projectId}/generate`, { request: msg });
    else run(`/api/projects/${projectId}/chat`, { message: msg, selected });
  };

  const approve = async () => {
    if (!rev) return;
    try {
      await api.approve(projectId, rev.revision);
      await refresh(rev.revision);
      setRev(await api.revision(projectId, rev.revision));
      setSvg(await api.svg(projectId, rev.revision));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const select = (tag: string | null) => {
    setSelected(tag);
    if (tag) setTab("drawing");
  };

  const status = rev?.status;
  const isLatest = rev && project && rev.revision === project.current;
  const v = rev?.validation.summary;

  return (
    <div className="app">
      <header className="topbar">
        <button className="btn small side-toggle" onClick={toggleSidebar} aria-label={sidebar ? "Hide drafts and versions" : "Show drafts and versions"} aria-expanded={sidebar} title="Drafts and versions">
          ☰
        </button>
        <button className="brand btn" style={{ border: 0, padding: 0, background: "none" }} onClick={onExit} title="All projects">
          <span className="brand-mark">CP</span> CadPilot
        </button>
        <span className="crumb">
          <b>{project?.name ?? "…"}</b> · {project?.drawing_number}
        </span>
        <button className="btn small" onClick={() => setShowData(true)} disabled={!project || !!live} title="View or replace the mass balance and design data workbooks">
          Design data
        </button>
        <span className="spacer" />
        {llm !== null && <span className={`badge ${llm ? "info" : "plain"} hide-sm`} title={llm ? "LLM reasoning enabled" : "Rule-based agents (no LLM key configured)"}>{llm ? "LLM agents" : "Rule agents"}</span>}
        {rev && <span className="mono hide-sm" style={{ color: "var(--muted)" }}>rev {rev.revision}{isLatest ? " (current)" : ""}</span>}
        {status && (
          <span className={`badge ${status === "approved" ? "ok" : status === "needs_attention" ? "err" : status === "superseded" ? "plain" : v?.warnings ? "warn" : "ok"}`}>
            {status === "ready_for_review" ? (v?.warnings ? `Ready · ${v.warnings} warning${v.warnings > 1 ? "s" : ""}` : "Ready for review") : status.replace(/_/g, " ")}
          </span>
        )}
        {rev && !isLatest && (
          <button
            className="btn small"
            disabled={!!live}
            title={`Bring back the design of revision ${rev.revision} as a new revision`}
            onClick={() => run(`/api/projects/${projectId}/revisions/${rev.revision}/restore`, {})}
          >
            Restore {rev.revision}
          </button>
        )}
        {rev && (
          <>
            <a className="btn small hide-sm" href={api.modelUrl(projectId, rev.revision)}>Model JSON</a>
            <a className="btn small" href={api.dxfUrl(projectId, rev.revision)} title={`Download revision ${rev.revision} as DXF (AutoCAD, layered, millimetres)`}>DXF</a>
            <a className="btn small hide-sm" href={api.svgUrl(projectId, rev.revision)} target="_blank" rel="noreferrer">SVG</a>
            <button className="btn good small" disabled={!isLatest || status === "approved" || !v?.passed || !!live} onClick={approve}>
              {status === "approved" ? "Approved" : "Approve"}
            </button>
          </>
        )}
      </header>

      <div className={`body${sidebar ? "" : " collapsed"}`}>
      <Sidebar
        projectId={projectId}
        current={project?.current ?? null}
        viewing={revLetter}
        refreshKey={`${project?.revisions.join(",")}|${rev?.revision}:${rev?.status}|${live ? 1 : 0}`}
        busy={!!live}
        onSelectRevision={(r) => {
          setRevLetter(r);
          if (isNarrow()) setSidebar(false);
        }}
        onOpenProject={onOpenProject}
        onNewDraft={onExit}
      />
      <div className="split">
        <Chat
          messages={project?.chat ?? []}
          live={live}
          input={input}
          setInput={setInput}
          onSend={send}
          selected={selected}
          clearSelected={() => setSelected(null)}
          onOpenRevision={(r) => {
            setRevLetter(r);
            setTab("changes");
          }}
          busy={!!live}
          hasDraft={!!project?.current}
        />

        <section className="viewer">
          <nav className="tabs" role="tablist">
            {([
              ["drawing", "P&ID"],
              ["validation", "Validation"],
              ["changes", "Changes"],
              ["lists", "Lists"],
              ["decisions", "Decisions"],
            ] as [Tab, string][]).map(([id, label]) => (
              <button key={id} className={`tab${tab === id ? " active" : ""}`} onClick={() => setTab(id)} role="tab" aria-selected={tab === id}>
                {label}
                {id === "validation" && v && (v.errors || v.warnings) ? (
                  <span className={`count ${v.errors ? "err" : "warn"}`}>{v.errors || v.warnings}</span>
                ) : null}
                {id === "changes" && rev?.changed_tags.length ? <span className="count">{rev.changed_tags.length}</span> : null}
              </button>
            ))}
            {rev && (
              <span className="zoom" style={{ color: "var(--muted)", fontSize: 12.5 }}>
                {rev.counts.equipment} equipment · {rev.counts.lines} lines · {rev.counts.instruments} instruments · {rev.counts.control_loops} loops
              </span>
            )}
          </nav>

          {error && <div className="error-banner" style={{ margin: 12 }}>{error}</div>}

          {!rev ? (
            <div className="stage" style={{ display: "grid", placeItems: "center", color: "var(--muted)" }}>
              {live ? (
                "The agents are drafting the first P&ID…"
              ) : project && !project.source_summary?.equipment_rows ? (
                <button className="btn primary" onClick={() => setShowData(true)}>Upload design data to start</button>
              ) : (
                "Loading…"
              )}
            </div>
          ) : tab === "drawing" ? (
            <div style={{ position: "relative", minHeight: 0, display: "grid" }}>
              <PidViewer svg={svg} selected={selected} onSelect={select} focusKey={`${projectId}:${rev.revision}`} />
              {selected && (
                <Inspector
                  rev={rev}
                  tag={selected}
                  onSelect={select}
                  onClose={() => setSelected(null)}
                  onAsk={(text, now) => (now ? send(text) : setInput(text))}
                />
              )}
            </div>
          ) : tab === "validation" ? (
            <ValidationPanel rev={rev} onSelect={select} />
          ) : tab === "changes" ? (
            <ChangesPanel rev={rev} onSelect={select} />
          ) : tab === "lists" ? (
            <ListsPanel rev={rev} onSelect={select} />
          ) : (
            <DecisionsPanel rev={rev} projectId={projectId} />
          )}
        </section>
      </div>
      </div>
      {showData && project && (
        <DataPanel
          project={project}
          onClose={() => setShowData(false)}
          onUploaded={setProject}
          onRegenerate={(engine, modules) =>
            run(`/api/projects/${projectId}/generate`, {
              engine,
              modules,
              request: project.current ? "Regenerate the draft from the updated design data" : initialRequest || "Generate the P&ID",
            })
          }
        />
      )}
    </div>
  );
}
