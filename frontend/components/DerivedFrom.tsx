"use client";

import { useEffect, useState } from "react";
import { api, type Trace } from "@/lib/api";

/** "Derived from": value → calculation → basis → source file / sheet / row, for one item. */
export default function DerivedFrom({ projectId, revision, tag }: { projectId: string; revision: string; tag: string }) {
  const [trace, setTrace] = useState<Trace | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    setTrace(null);
    setFailed(false);
    api.trace(projectId, revision, tag).then((t) => live && setTrace(t)).catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, [projectId, revision, tag]);

  if (failed) return null;
  return (
    <div>
      <p className="section-title">Derived from</p>
      {!trace ? (
        <div className="module-hint">Tracing…</div>
      ) : (
        <ol className="trace">
          {trace.steps.map((s, k) => (
            <li key={k}>
              <span className="trace-label">{s.label}</span>
              <span className="trace-value">{s.value}</span>
              {s.detail && <span className="trace-detail">{s.detail}</span>}
              {s.source && (
                <span className="trace-source" title={s.source.text}>
                  {s.source.file} › {s.source.sheet}
                  {s.source.row ? ` › row ${s.source.row}` : ""}
                  {s.source.text && <em> — “{s.source.text.slice(0, 90)}”</em>}
                </span>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
