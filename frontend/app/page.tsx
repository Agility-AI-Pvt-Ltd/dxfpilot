"use client";

import { useCallback, useEffect, useState } from "react";
import StartScreen from "@/components/StartScreen";
import Workspace from "@/components/Workspace";
import { api, type Engine, type Project } from "@/lib/api";

type Open = { id: string; request: string; engine?: Engine; modules?: string[]; detect?: boolean } | null;

/** The URL is the source of truth for which draft is open: ?p=<id>, or no ?p for the new-draft screen.
 *  A project the CRM handed over (the CRM links to ?p=<id>&from=crm) shows the prefilled new-draft
 *  page until the reviewer has chosen the options and started its first draft. */
function fromUrl(): Open {
  const id = new URLSearchParams(window.location.search).get("p");
  return id ? { id, request: "" } : null;
}

export default function Home() {
  const [project, setProject] = useState<Open>(null);
  const [previous, setPrevious] = useState<string | null>(null); // draft to return to from "New draft"
  const [ready, setReady] = useState(false);
  // CRM hand-over waiting for its first draft; `checked` = the project id this was worked out for
  const [handover, setHandover] = useState<Project | null>(null);
  const [checked, setChecked] = useState<string | null>(null);

  useEffect(() => {
    setProject(fromUrl());
    setReady(true);
    // Browser back/forward: follow the URL
    const onPop = () => setProject(fromUrl());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // Opened by link, back/forward or the recent list (no request yet): a CRM project without a draft
  // gets the prefilled new-draft page. Nothing renders until this is known, so the workspace never
  // mounts first and starts a draft with default options.
  useEffect(() => {
    if (!project) return;
    if (project.request) {
      setHandover(null);
      setChecked(project.id);
      return;
    }
    let cancelled = false;
    api.project(project.id).then(
      (p) => {
        if (cancelled) return;
        setHandover(p.integration && !p.current ? p : null);
        setChecked(project.id);
      },
      () => {
        if (cancelled) return;
        setHandover(null); // unknown project: the workspace shows the error
        setChecked(project.id);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [project]);

  const open = useCallback((id: string, request: string, engine?: Engine, modules?: string[], detect?: boolean) => {
    window.history.pushState(null, "", `?p=${id}`);
    setProject({ id, request, engine, modules, detect });
  }, []);

  const newDraft = useCallback(() => {
    setPrevious(project?.id ?? null);
    window.history.pushState(null, "", "/");
    setProject(null);
  }, [project]);

  const cancelNewDraft = useCallback(() => {
    // Prefer a real "back" so the history stays tidy; fall back to reopening the previous draft
    if (previous && window.history.length > 1) window.history.back();
    else if (previous) open(previous, "");
  }, [previous, open]);

  if (!ready) return null;
  if (!project) return <StartScreen onReady={open} onCancel={previous ? cancelNewDraft : undefined} />;
  if (checked !== project.id) return null; // still finding out whether this is a CRM hand-over
  if (handover) return <StartScreen key={handover.id} handover={handover} onReady={open} />;
  return (
    <Workspace
      key={project.id}
      projectId={project.id}
      initialRequest={project.request}
      initialEngine={project.engine}
      initialModules={project.modules}
      initialDetect={project.detect}
      onOpenProject={(id) => open(id, "")}
      onExit={newDraft}
    />
  );
}
