"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

type Props = {
  svg: string;
  selected: string | null;
  onSelect: (tag: string | null) => void;
  focusKey: string; // changes when a new drawing should be re-fitted
};

type View = { s: number; x: number; y: number };

export default function PidViewer({ svg, selected, onSelect, focusKey }: Props) {
  const stageRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<View>({ s: 1, x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; vx: number; vy: number; moved: boolean } | null>(null);
  const [dragging, setDragging] = useState(false);
  const userMoved = useRef(false); // once the reviewer zooms/pans, stop auto-fitting on resize

  const fit = useCallback(() => {
    const stage = stageRef.current;
    const svgEl = innerRef.current?.querySelector("svg");
    if (!stage || !svgEl) return;
    const W = Number(svgEl.getAttribute("width")) || 1;
    const H = Number(svgEl.getAttribute("height")) || 1;
    const r = stage.getBoundingClientRect();
    if (r.width < 40 || r.height < 40) return;
    userMoved.current = false;
    const s = Math.min((r.width - 32) / W, (r.height - 32) / H);
    setView({ s, x: (r.width - W * s) / 2, y: (r.height - H * s) / 2 });
  }, []);

  useLayoutEffect(() => {
    fit();
  }, [focusKey, fit]);

  // Re-fit when the viewing area changes size (sidebar toggled, window resized) — unless the
  // reviewer has zoomed or panned themselves.
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (!userMoved.current) fit();
    });
    ro.observe(stage);
    return () => ro.disconnect();
  }, [fit]);

  // Selection highlight lives in the DOM of the injected SVG
  useEffect(() => {
    const root = innerRef.current;
    if (!root) return;
    root.querySelectorAll(".selected").forEach((el) => el.classList.remove("selected"));
    if (selected) {
      root.querySelectorAll(`[data-tag="${CSS.escape(selected)}"]`).forEach((el) => el.classList.add("selected"));
    }
  }, [selected, svg]);

  const zoomAt = (factor: number, cx: number, cy: number) => {
    userMoved.current = true;
    setView((v) => {
      const s = Math.min(6, Math.max(0.1, v.s * factor));
      const k = s / v.s;
      return { s, x: cx - (cx - v.x) * k, y: cy - (cy - v.y) * k };
    });
  };

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = stage.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey || Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        zoomAt(Math.exp(-e.deltaY * 0.0018), e.clientX - r.left, e.clientY - r.top);
      } else {
        userMoved.current = true;
        setView((v) => ({ ...v, x: v.x - e.deltaX, y: v.y - e.deltaY }));
      }
    };
    stage.addEventListener("wheel", onWheel, { passive: false });
    return () => stage.removeEventListener("wheel", onWheel);
  }, []);

  const onPointerDown = (e: React.PointerEvent) => {
    drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) > 4) {
      d.moved = true;
      setDragging(true);
      (e.target as Element).setPointerCapture?.(e.pointerId);
    }
    if (d.moved) {
      userMoved.current = true;
      setView((v) => ({ ...v, x: d.vx + dx, y: d.vy + dy }));
    }
  };
  const onPointerUp = (e: React.PointerEvent) => {
    const d = drag.current;
    drag.current = null;
    setDragging(false);
    if (d && !d.moved) {
      const el = (e.target as Element).closest?.("[data-tag]");
      onSelect(el ? el.getAttribute("data-tag") : null);
    }
  };

  const center = () => {
    const r = stageRef.current?.getBoundingClientRect();
    return r ? [r.width / 2, r.height / 2] : [0, 0];
  };

  return (
    <div className="stage" ref={stageRef}>
      <div
        className={`canvas${dragging ? " dragging" : ""}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={() => {
          drag.current = null;
          setDragging(false);
        }}
      >
        <div
          ref={innerRef}
          className="canvas-inner"
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.s})` }}
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </div>
      <div className="hint">Scroll to zoom · drag to pan · click an item to inspect</div>
      <div className="zoom" style={{ position: "absolute", right: 12, bottom: 12, background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 8, padding: 4 }}>
        <button className="btn small" onClick={() => zoomAt(1 / 1.25, ...(center() as [number, number]))} aria-label="Zoom out">−</button>
        <span>{Math.round(view.s * 100)}%</span>
        <button className="btn small" onClick={() => zoomAt(1.25, ...(center() as [number, number]))} aria-label="Zoom in">+</button>
        <button className="btn small" onClick={fit}>Fit</button>
      </div>
    </div>
  );
}
