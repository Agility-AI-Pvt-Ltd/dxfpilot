"use client";

import { useCallback, useEffect, useState } from "react";
import StartScreen from "@/components/StartScreen";
import Workspace from "@/components/Workspace";

type Open = { id: string; request: string } | null;

/** The URL is the source of truth for which draft is open: ?p=<id>, or no ?p for the new-draft screen. */
function fromUrl(): Open {
  const id = new URLSearchParams(window.location.search).get("p");
  return id ? { id, request: "" } : null;
}

export default function Home() {
  const [project, setProject] = useState<Open>(null);
  const [previous, setPrevious] = useState<string | null>(null); // draft to return to from "New draft"
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setProject(fromUrl());
    setReady(true);
    // Browser back/forward: follow the URL
    const onPop = () => setProject(fromUrl());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const open = useCallback((id: string, request: string) => {
    window.history.pushState(null, "", `?p=${id}`);
    setProject({ id, request });
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
  return (
    <Workspace
      key={project.id}
      projectId={project.id}
      initialRequest={project.request}
      onOpenProject={(id) => open(id, "")}
      onExit={newDraft}
    />
  );
}
