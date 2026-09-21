"use client";

import { useEffect, useRef } from "react";
import type { ChatMessage, LLMCallSummary, ProgressEvent } from "@/lib/api";

/** Who produced this reply: the configured model, or the deterministic rules (and why). */
function Source({ interpreter, llm }: { interpreter?: string; llm?: LLMCallSummary[] }) {
  if (!interpreter && !llm) return null;
  const ok = (llm ?? []).filter((c) => c.outcome === "ok");
  const failed = (llm ?? []).filter((c) => c.outcome !== "ok" && c.outcome !== "cached");
  const cached = (llm ?? []).filter((c) => c.outcome === "cached").length;
  const short = (m: string) => (m.length > 28 ? `${m.slice(0, 26)}…` : m);
  const bits: string[] = [];
  if (ok.length) bits.push(`LLM ${ok.length}× · ${short(ok[0].model)} · ${(ok.reduce((a, c) => a + c.ms, 0) / 1000).toFixed(1)} s`);
  if (cached) bits.push(`${cached} reused from cache`);
  if (failed.length) bits.push(`${failed.length} LLM call${failed.length > 1 ? "s" : ""} failed (${failed.map((c) => c.outcome).join(", ")})`);
  if (!llm?.length) bits.push("no LLM call");
  const rules = interpreter?.startsWith("rules");
  return (
    <span className={`source ${ok.length && !rules ? "llm" : "rules"}`} title={(llm ?? []).map((c) => `${c.purpose}: ${c.outcome} (${c.model}, ${c.ms} ms)`).join("\n")}>
      {interpreter ? `interpreted by ${interpreter} · ` : ""}
      {bits.join(" · ")}
    </span>
  );
}

const STEP_LABEL: Record<string, string> = {
  plan: "Analyzing design data",
  process_agent: "Process requirements identified",
  equipment_agent: "Equipment identified",
  reconcile: "Proposals reconciled",
  topology_agent: "Process topology generated",
  instrumentation_agent: "Instrumentation generated",
  merge: "Engineering model assembled",
  validate: "Engineering rules checked",
  commit: "Model committed",
  layout: "Layout computed",
  render: "P&ID generated",
  interpret: "Correction understood",
};

export function Steps({ events, running }: { events: ProgressEvent[]; running?: boolean }) {
  return (
    <ul className="steps">
      {events.map((e, k) => (
        <li key={k} title={e.detail}>
          <span className="tick">✓</span>
          <span>
            {e.detail || STEP_LABEL[e.step] || e.step}
            {e.llm && <span className="llm">LLM</span>}
          </span>
        </li>
      ))}
      {running && (
        <li>
          <span className="spin" />
          <span>Working…</span>
        </li>
      )}
    </ul>
  );
}

type Props = {
  messages: ChatMessage[];
  live: ProgressEvent[] | null;
  input: string;
  setInput: (s: string) => void;
  onSend: (text?: string) => void;
  selected: string | null;
  clearSelected: () => void;
  onOpenRevision: (rev: string) => void;
  busy: boolean;
  hasDraft: boolean;
};

const SUGGESTIONS = [
  "The pasteurizer should be before the separator",
  "Change E-101 to 30 KLPH",
  "Use 4 silos",
  "Add a bypass around F-201",
  "Why is FT-103 required?",
];

export default function Chat({ messages, live, input, setInput, onSend, selected, clearSelected, onOpenRevision, busy, hasDraft }: Props) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [messages.length, live?.length]);

  return (
    <aside className="chat">
      <div className="messages" aria-live="polite">
        {messages.map((m, k) => (
          <div key={k} className={`msg ${m.role}`}>
            {m.text}
            {m.role === "assistant" && m.data?.events && m.data.events.length > 0 && (
              <details style={{ marginTop: 6 }}>
                <summary style={{ fontSize: 12, color: "var(--muted)", cursor: "pointer" }}>{m.data.events.length} agent steps</summary>
                <Steps events={m.data.events} />
              </details>
            )}
            {m.role === "assistant" && (m.data?.interpreter || m.data?.llm) && (
              <div className="meta"><Source interpreter={m.data.interpreter} llm={m.data.llm} /></div>
            )}
            {(m.revision || m.data?.selected) && (
              <div className="meta">
                {m.data?.selected && <span>on {m.data.selected}</span>}
                {m.revision && m.role === "assistant" && (
                  <button className="link" onClick={() => onOpenRevision(m.revision!)}>Open revision {m.revision} →</button>
                )}
              </div>
            )}
          </div>
        ))}
        {live && (
          <div className="msg assistant">
            {hasDraft ? "Updating the engineering model…" : "Generating the first draft…"}
            <Steps events={live} running />
          </div>
        )}
        <div ref={end} />
      </div>
      <div className="composer">
        {hasDraft && !busy && messages.length < 4 && (
          <div className="suggestions">
            {SUGGESTIONS.map((s) => (
              <button key={s} className="suggestion" onClick={() => setInput(s)}>{s}</button>
            ))}
          </div>
        )}
        <textarea
          value={input}
          placeholder={hasDraft ? "Review the draft. Describe a correction or ask why…" : "Describe what to generate…"}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSend();
            }
          }}
          disabled={busy}
          aria-label="Message CadPilot"
        />
        <div className="composer-row">
          {selected && (
            <span className="chip">
              {selected}
              <button onClick={clearSelected} aria-label="Clear selection">×</button>
            </span>
          )}
          <span className="spacer" />
          <button className="btn primary" disabled={busy || !input.trim()} onClick={() => onSend()}>
            {busy ? "Working…" : hasDraft ? "Send" : "Generate"}
          </button>
        </div>
      </div>
    </aside>
  );
}
