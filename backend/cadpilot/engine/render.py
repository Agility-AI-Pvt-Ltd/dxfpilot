"""P&ID SVG renderer. Reads only the engineering model + layout + symbol library.

Every drawn object carries data-tag / data-kind so the web viewer can select, inspect and
highlight it. Styling uses CSS variables (with fallbacks) so the sheet follows the app theme.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from html import escape

from ..model.engineering import EngineeringModel, Instrument, LayoutPoint, Line
from ..standards import Standards
from .tags import Tagger, TagRegistry

STYLE = """
.pid{font-family:'IBM Plex Sans','Segoe UI',Arial,sans-serif}
.pid .sheet{fill:var(--pid-paper,#fbfbf8)}
.pid .sym{fill:var(--pid-paper,#fbfbf8);stroke:var(--pid-ink,#1b2230);stroke-width:1.7}
.pid .sym-bg{fill:var(--pid-paper,#fbfbf8);stroke:var(--pid-ink,#1b2230);stroke-width:1.3}
.pid .sym-thin{fill:none;stroke:var(--pid-ink,#1b2230);stroke-width:1}
.pid .sym-fill{fill:var(--pid-ink,#1b2230);stroke:none}
.pid .sym-note{font-size:9px;fill:var(--pid-muted,#5b6475)}
.pid .pipe{fill:none;stroke:var(--pid-ink,#1b2230);stroke-width:1.8}
.pid .pipe.bypass{stroke-dasharray:7 4}
.pid .pipe-hit{fill:none;stroke:transparent;stroke-width:12;cursor:pointer}
.pid .header{stroke:var(--pid-ink,#1b2230);stroke-width:4}
.pid .signal{fill:none;stroke:var(--pid-signal,#2f6db5);stroke-width:1;stroke-dasharray:5 3}
.pid .leader{fill:none;stroke:var(--pid-ink,#1b2230);stroke-width:.8}
.pid .bubble{fill:var(--pid-paper,#fbfbf8);stroke:var(--pid-ink,#1b2230);stroke-width:1.2}
.pid .tag{font-size:12px;font-weight:600;fill:var(--pid-ink,#1b2230)}
.pid .sub{font-size:9.5px;fill:var(--pid-muted,#5b6475)}
.pid .ln-label{font-size:9px;fill:var(--pid-muted,#5b6475)}
.pid .fn{font-size:9.5px;font-weight:600;fill:var(--pid-ink,#1b2230)}
.pid .loopno{font-size:8.5px;fill:var(--pid-ink,#1b2230)}
.pid .alarm{font-size:8px;fill:var(--pid-alarm,#b0412e)}
.pid .area{fill:none;stroke:var(--pid-muted,#8a93a3);stroke-width:1;stroke-dasharray:10 6}
.pid .area-label{font-size:11px;letter-spacing:.08em;fill:var(--pid-muted,#5b6475)}
.pid .arrow{fill:var(--pid-ink,#1b2230)}
.pid .tb{fill:var(--pid-paper,#fbfbf8);stroke:var(--pid-ink,#1b2230);stroke-width:1.2}
.pid .tb-t{font-size:11px;fill:var(--pid-ink,#1b2230)}
.pid .tb-b{font-size:14px;font-weight:700;fill:var(--pid-ink,#1b2230)}
.pid .item{cursor:pointer}
.pid .changed .sym,.pid .changed .bubble,.pid .changed .sym-bg{stroke:var(--pid-change,#d2462f)}
.pid .changed.pipe,.pid .pipe.changed{stroke:var(--pid-change,#d2462f)}
.pid .cloud{fill:none;stroke:var(--pid-change,#d2462f);stroke-width:1.4;stroke-dasharray:3 3}
.pid .selected .sym,.pid .selected .bubble,.pid .pipe.selected{stroke:var(--pid-select,#2f6db5);stroke-width:2.6}
"""

Pt = tuple[float, float]


def _f(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


class _Renderer:
    def __init__(self, model: EngineeringModel, standards: Standards, highlight: set[str]):
        self.m = model
        self.s = standards
        self.hl = highlight
        # an item the layout could not place (e.g. a planned header connected to nothing) is still drawn
        self.pos = {t: model.layout.positions.get(t) or LayoutPoint(x=40, y=40) for t in
                    [e.tag for e in model.equipment] + [n.tag for n in model.piping_nodes]} | dict(model.layout.positions)
        self.kinds = {e.tag: e.type for e in model.equipment} | {n.tag: n.kind for n in model.piping_nodes}
        self.out: list[str] = []
        self.routes: dict[str, list[Pt]] = {}
        self.anchors: dict[str, Pt] = {}  # inline component tag -> point

    # ---- geometry --------------------------------------------------------------------------
    def size(self, tag: str) -> tuple[float, float]:
        k = self.kinds.get(tag, "")
        if k in self.s.symbols["equipment"]:
            w, h = self.s.symbols["equipment"][k]["size"]
            return w, h
        if k in self.s.symbols["piping_nodes"]:
            w, h = self.s.symbols["piping_nodes"][k]["size"]
            return w, h
        return 8, 8

    def port(self, tag: str, port: str) -> Pt:
        p = self.pos.get(tag) or LayoutPoint(x=40, y=40)
        spec = self.s.ports_for(self.kinds.get(tag, "")).get(port)
        if spec:
            return p.x + spec["x"], p.y + spec["y"]
        return p.x, p.y

    def route(self, ln: Line) -> list[Pt]:
        fk, tk = self.kinds.get(ln.from_.item), self.kinds.get(ln.to.item)
        a = self.port(ln.from_.item, ln.from_.port)
        b = self.port(ln.to.item, ln.to.port)
        if ln.kind == "bypass":
            yb = min(a[1], b[1]) - self.s.layout["bypass_offset_y"]
            return [a, (a[0], yb), (b[0], yb), b]
        if fk == "header":
            a = (a[0], b[1])
        if tk == "header":
            b = (b[0], a[1])
        if fk == "tee":
            a = (a[0], b[1]) if tk != "header" else a
        if tk == "tee":
            b = (b[0], a[1])
        if abs(a[1] - b[1]) < 0.5:
            return [a, b]
        xm = b[0] - 16 if b[0] - a[0] > 40 else a[0] + (b[0] - a[0]) / 2
        if fk == "header":
            xm = a[0] + 12
        elif tk == "header":
            xm = b[0] - 12
        return [a, (xm, a[1]), (xm, b[1]), b]

    @staticmethod
    def longest_horizontal(pts: list[Pt]) -> tuple[Pt, Pt]:
        segs = [(p, q) for p, q in zip(pts, pts[1:])]
        horiz = [s for s in segs if abs(s[0][1] - s[1][1]) < 0.5] or segs
        return max(horiz, key=lambda s: abs(s[1][0] - s[0][0]) + abs(s[1][1] - s[0][1]))

    # ---- drawing ---------------------------------------------------------------------------
    def cls(self, base: str, tag: str) -> str:
        return f"{base} changed" if tag in self.hl else base

    def draw_areas(self) -> None:
        areas = self.s.template(self.m.process.template).get("areas", {})
        by_area: dict[str, list[str]] = defaultdict(list)
        for e in self.m.equipment:
            by_area[e.area].append(e.tag)
        for n in self.m.piping_nodes:
            if n.kind not in ("header", "tee"):
                by_area[n.area].append(n.tag)
        pad_x, pad_top = 40, 150
        boxes = []
        for area, tags in sorted(by_area.items(), key=lambda kv: (len(kv[0]), kv[0])):
            xs = [self.pos[t].x - self.size(t)[0] / 2 for t in tags] + [self.pos[t].x + self.size(t)[0] / 2 for t in tags]
            ys = [self.pos[t].y for t in tags]
            bottom = max(self.pos[t].y + self.size(t)[1] / 2 + 52 for t in tags)  # below the tag and name
            boxes.append([area, min(xs) - pad_x, min(ys) - pad_top, max(xs) + pad_x, max(bottom, max(ys) + 90)])
        for i in range(len(boxes) - 1):  # no overlap between side-by-side areas (module bands stack)
            same_band = boxes[i][2] < boxes[i + 1][4] and boxes[i + 1][2] < boxes[i][4]
            if same_band and boxes[i][3] > boxes[i + 1][1] - 6:
                midx = (boxes[i][3] + boxes[i + 1][1]) / 2
                boxes[i][3], boxes[i + 1][1] = midx - 3, midx + 3
        for area, x0, y0, x1, y1 in boxes:
            self.out.append(
                f'<rect class="area" x="{_f(x0)}" y="{_f(y0)}" width="{_f(x1 - x0)}" height="{_f(y1 - y0)}" rx="6"/>'
                f'<text class="area-label" x="{_f(x0 + 10)}" y="{_f(y0 + 16)}">AREA {escape(area)} — {escape(str(areas.get(area, "")).upper())}</text>'
            )

    def draw_lines(self) -> None:
        tagger = Tagger(self.s, TagRegistry())
        header_spans: dict[str, list[float]] = defaultdict(list)
        for ln in self.m.lines:
            pts = self.route(ln)
            self.routes[ln.tag] = pts
            for end, p in ((ln.from_.item, pts[0]), (ln.to.item, pts[-1])):
                if self.kinds.get(end) == "header":
                    header_spans[end].append(p[1])
            d = " ".join(f"{'M' if i == 0 else 'L'}{_f(x)},{_f(y)}" for i, (x, y) in enumerate(pts))
            klass = "pipe bypass" if ln.kind == "bypass" else "pipe"
            (sa, sb) = self.longest_horizontal(pts)
            label = tagger.line_label(ln.tag, ln.size_dn, ln.spec)
            lx = min(sa[0], sb[0]) + 4
            ly = sa[1] - 5
            arrow = ""
            if abs(sa[1] - sb[1]) < 0.5 and abs(sb[0] - sa[0]) > 50:
                ax = sa[0] + (sb[0] - sa[0]) * 0.92
                direction = 1 if sb[0] > sa[0] else -1
                arrow = f'<path class="arrow" d="M{_f(ax)},{_f(sa[1])} l{-7 * direction},-4 l0,8 z"/>'
            self.out.append(
                f'<g class="item" data-tag="{escape(ln.tag)}" data-kind="line">'
                f'<path class="pipe-hit" d="{d}"/><path class="{self.cls(klass, ln.tag)}" d="{d}"/>{arrow}'
                f'<text class="ln-label" x="{_f(lx)}" y="{_f(ly)}">{escape(label)}</text></g>'
            )
            self.draw_inline(ln, sa, sb)
        for hdr, ys in header_spans.items():
            x = self.pos[hdr].x
            y0, y1 = min(ys), max(ys)
            self.out.append(
                f'<g class="item" data-tag="{escape(hdr)}" data-kind="piping_node">'
                f'<line class="{self.cls("header", hdr)}" x1="{_f(x)}" y1="{_f(y0 - 6)}" x2="{_f(x)}" y2="{_f(y1 + 6)}"/>'
                f'<text class="sub" x="{_f(x + 6)}" y="{_f(y1 + 18)}">{escape(hdr)}</text></g>'
            )

    def draw_inline(self, ln: Line, sa: Pt, sb: Pt) -> None:
        comps = ln.inline
        if not comps:
            return
        horizontal = abs(sa[1] - sb[1]) < 0.5
        n = len(comps)
        x0, x1 = min(sa[0], sb[0]), max(sa[0], sb[0])
        label_w = self.s.layout["line_label_width"]
        start = x0 + label_w if x1 - x0 > label_w + 20 * n else x0 + 10
        for i, comp in enumerate(comps):
            if horizontal:
                x = start + (x1 - 12 - start) * (i + 0.5) / n
                y = sa[1]
            else:
                frac = (i + 1) / (n + 1)
                x = sa[0] + (sb[0] - sa[0]) * frac
                y = sa[1] + (sb[1] - sa[1]) * frac
            self.anchors[comp.tag] = (x, y)
            g = self.s.symbols["inline"].get(comp.type, {}).get("graphics", "")
            rot = "" if horizontal else " rotate(90)"
            self.out.append(
                f'<g class="item{" changed" if comp.tag in self.hl else ""}" data-tag="{escape(comp.tag)}" data-kind="inline">'
                f'<g transform="translate({_f(x)},{_f(y)}){rot}">{g}</g>'
                f'<text class="sub" x="{_f(x)}" y="{_f(y + 22)}" text-anchor="middle">{escape(comp.tag)}</text></g>'
            )

    def draw_nodes(self) -> None:
        for e in self.m.equipment:
            p = self.pos[e.tag]
            w, h = self.size(e.tag)
            g = self.s.symbols["equipment"][e.type]["graphics"]
            cap = f" · {e.capacity}" if e.capacity else ""
            cloud = ""
            if e.tag in self.hl:
                cloud = f'<rect class="cloud" x="{_f(p.x - w / 2 - 10)}" y="{_f(p.y - h / 2 - 10)}" width="{_f(w + 20)}" height="{_f(h + 58)}" rx="14"/>'
            self.out.append(
                f'<g class="{self.cls("item", e.tag)}" data-tag="{escape(e.tag)}" data-kind="equipment">{cloud}'
                f'<g transform="translate({_f(p.x)},{_f(p.y)})">{g}</g>'
                f'<text class="tag" x="{_f(p.x)}" y="{_f(p.y + h / 2 + 22)}" text-anchor="middle">{escape(e.tag)}</text>'
                f'<text class="sub" x="{_f(p.x)}" y="{_f(p.y + h / 2 + 35)}" text-anchor="middle">{escape(_short(e.name))}{escape(cap)}</text></g>'
            )
        for n in self.m.piping_nodes:
            p = self.pos[n.tag]
            if n.kind == "tee":
                self.out.append(
                    f'<g class="item" data-tag="{escape(n.tag)}" data-kind="piping_node">'
                    f'<circle class="sym-fill" cx="{_f(p.x)}" cy="{_f(p.y)}" r="3.5"/></g>'
                )
            elif n.kind in ("terminal_in", "terminal_out"):
                rows = _wrap(n.label, 19, 3)
                w, h = 120, max(26, 11 * len(rows) + 6)
                x0 = p.x - w / 2
                shape = (
                    f"M{_f(x0)},{_f(p.y - h / 2)} L{_f(x0 + w - 12)},{_f(p.y - h / 2)} L{_f(x0 + w)},{_f(p.y)} "
                    f"L{_f(x0 + w - 12)},{_f(p.y + h / 2)} L{_f(x0)},{_f(p.y + h / 2)} Z"
                )
                y_first = p.y - (len(rows) - 1) * 5.5 + 3.5
                text = "".join(
                    f'<text class="sub" x="{_f(x0 + 6)}" y="{_f(y_first + k * 11)}">{escape(row)}</text>' for k, row in enumerate(rows)
                )
                self.out.append(
                    f'<g class="{self.cls("item", n.tag)}" data-tag="{escape(n.tag)}" data-kind="piping_node">'
                    f'<path class="sym" d="{shape}"/>{text}'
                    f'<text class="tag" x="{_f(p.x)}" y="{_f(p.y - h / 2 - 6)}" text-anchor="middle">{escape(n.tag)}</text></g>'
                )

    def bubble(self, inst: Instrument, x: float, y: float) -> str:
        r = 16
        fn, _, loop = inst.tag.partition("-")
        shape = f'<circle class="bubble" cx="{_f(x)}" cy="{_f(y)}" r="{r}"/>'
        if inst.location == "dcs":
            shape = f'<rect class="bubble" x="{_f(x - r)}" y="{_f(y - r)}" width="{2 * r}" height="{2 * r}"/>' + shape
        elif inst.location == "local_panel":
            shape += f'<line class="leader" x1="{_f(x - r)}" y1="{_f(y)}" x2="{_f(x + r)}" y2="{_f(y)}"/>'
        alarms = ""
        if inst.alarms:
            alarms = f'<text class="alarm" x="{_f(x)}" y="{_f(y - r - 3)}" text-anchor="middle">{escape(" ".join(inst.alarms))}</text>'
        return (
            f'<g class="{self.cls("item", inst.tag)}" data-tag="{escape(inst.tag)}" data-kind="instrument">{shape}'
            f'<text class="fn" x="{_f(x)}" y="{_f(y - 1)}" text-anchor="middle">{escape(fn)}</text>'
            f'<text class="loopno" x="{_f(x)}" y="{_f(y + 10)}" text-anchor="middle">{escape(loop)}</text>{alarms}</g>'
        )

    def draw_instruments(self) -> None:
        cfg = self.s.layout
        groups: dict[str, list[Instrument]] = defaultdict(list)
        valves: list[Instrument] = []
        for i in self.m.instruments:
            (valves if i.type == "control_valve" else groups[i.attached_to.ref]).append(i)
        placed: dict[str, Pt] = {}
        for ref, insts in groups.items():
            if ref in self.pos and self.kinds.get(ref) == "silo":
                p = self.pos[ref]
                w, h = self.size(ref)
                sx = p.x + w / 2 + 34
                for k, inst in enumerate(insts):
                    y = p.y - h / 2 + 20 + k * 40
                    placed[inst.tag] = (sx, y)
                    self.out.append(f'<path class="leader" d="M{_f(p.x + w / 2)},{_f(y)} L{_f(sx - 16)},{_f(y)}"/>')
                    self.out.append(self.bubble(inst, sx, y))
                continue
            if ref in self.pos:
                p = self.pos[ref]
                _, h = self.size(ref)
                anchor = (p.x, p.y - h / 2)
                by = p.y - h / 2 - 52
            elif ref in self.routes:
                sa, sb = self.longest_horizontal(self.routes[ref])
                fe = next((self.anchors[c.tag] for ln in self.m.lines if ln.tag == ref for c in ln.inline if c.type == "flow_element" and c.tag in self.anchors), None)
                anchor = fe or ((sa[0] + sb[0]) / 2, (sa[1] + sb[1]) / 2)
                by = anchor[1] - 64
            else:
                continue
            n = len(insts)
            sp = cfg["instrument_spacing_x"]
            for k, inst in enumerate(insts):
                x = anchor[0] + (k - (n - 1) / 2) * sp
                placed[inst.tag] = (x, by)
            lead_x = anchor[0]
            self.out.append(f'<path class="leader" d="M{_f(lead_x)},{_f(by + 16)} L{_f(lead_x)},{_f(anchor[1])}"/>')
            for inst in insts:
                x, y = placed[inst.tag]
                self.out.append(self.bubble(inst, x, y))

        # Utility control valves beside their equipment
        for v in valves:
            ref = v.attached_to.ref
            if ref not in self.pos:
                continue
            p = self.pos[ref]
            w, h = self.size(ref)
            # utility supply enters at the top-right corner of the exchanger
            x, y = p.x + w / 2 + 14, p.y - h / 2 - 12
            util = "HW" if self.kinds.get(ref) == "pasteurizer" else "CHW"
            placed[v.tag] = (x, y - 20)
            self.out.append(
                f'<g class="{self.cls("item", v.tag)}" data-tag="{escape(v.tag)}" data-kind="instrument">'
                f'<path class="sym-thin" d="M{_f(p.x + w / 2 - 8)},{_f(p.y - h / 2)} L{_f(p.x + w / 2 - 8)},{_f(y)} L{_f(x + 34)},{_f(y)}"/>'
                f'<g transform="translate({_f(x)},{_f(y)})">{self.s.symbols["inline"]["control_valve"]["graphics"]}</g>'
                f'<text class="sub" x="{_f(x + 12)}" y="{_f(y + 17)}">{escape(v.tag)} {util}</text></g>'
            )

        # Signal lines for control loops
        for lp in self.m.loops:
            src = placed.get(lp.tag) or placed.get(lp.measured_by)
            if src is None:
                continue
            meas = placed.get(lp.measured_by)
            if meas and meas != src:
                self.out.append(f'<path class="signal" d="M{_f(meas[0] + 16)},{_f(meas[1])} L{_f(src[0] - 16)},{_f(src[1])}"/>')
            if lp.final_element_kind == "vfd" and lp.final_element in self.pos:
                p = self.pos[lp.final_element]
                _, h = self.size(lp.final_element)
                tgt = (p.x, p.y - h / 2 - 4)
                self.out.append(
                    f'<path class="signal" d="M{_f(src[0])},{_f(src[1] - 16)} L{_f(src[0])},{_f(src[1] - 30)} '
                    f'L{_f(tgt[0])},{_f(src[1] - 30)} L{_f(tgt[0])},{_f(tgt[1])}"/>'
                    f'<text class="alarm" x="{_f(tgt[0] + 6)}" y="{_f(tgt[1] - 4)}">VFD</text>'
                )
            else:
                tgt = placed.get(lp.final_element) or self.anchors.get(lp.final_element)
                if tgt:
                    ty = tgt[1] - 20 if lp.final_element in self.anchors else tgt[1]
                    self.out.append(
                        f'<path class="signal" d="M{_f(src[0] + 16)},{_f(src[1])} L{_f(tgt[0])},{_f(src[1])} L{_f(tgt[0])},{_f(ty)}"/>'
                    )

    def draw_title_block(self, revision: str, status: str) -> None:
        W, H = self.m.layout.width, self.m.layout.height
        w, h = 520, 92
        x, y = W - w - 30, H - h - 24
        c = self.m.counts()
        self.out.append(
            f'<g class="title-block"><rect class="tb" x="{_f(x)}" y="{_f(y)}" width="{w}" height="{h}"/>'
            f'<line class="tb" x1="{_f(x)}" y1="{_f(y + 30)}" x2="{_f(x + w)}" y2="{_f(y + 30)}"/>'
            f'<line class="tb" x1="{_f(x + 380)}" y1="{_f(y + 30)}" x2="{_f(x + 380)}" y2="{_f(y + h)}"/>'
            f'<text class="tb-b" x="{_f(x + 10)}" y="{_f(y + 21)}">P&amp;ID — {escape(self.m.process.name.upper())}</text>'
            f'<text class="tb-t" x="{_f(x + 10)}" y="{_f(y + 48)}">{escape(self.m.project.name)} · {escape(self.m.project.drawing_number)}</text>'
            f'<text class="tb-t" x="{_f(x + 10)}" y="{_f(y + 64)}">{c["equipment"]} equipment · {c["lines"]} lines · {c["instruments"]} instruments · {c["control_loops"]} loops</text>'
            f'<text class="tb-t" x="{_f(x + 10)}" y="{_f(y + 80)}">{escape(status)} · {date.today().isoformat()}</text>'
            f'<text class="tb-t" x="{_f(x + 392)}" y="{_f(y + 50)}">REV</text>'
            f'<text class="tb-b" x="{_f(x + 392)}" y="{_f(y + 76)}" style="font-size:26px">{escape(revision)}</text></g>'
        )

    def render(self, revision: str, status: str) -> str:
        W, H = self.m.layout.width, self.m.layout.height
        self.draw_areas()
        self.draw_lines()
        self.draw_nodes()
        self.draw_instruments()
        self.draw_title_block(revision, status)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" class="pid" viewBox="0 0 {_f(W)} {_f(H)}" '
            f'width="{_f(W)}" height="{_f(H)}"><style>{STYLE}</style>'
            f'<rect class="sheet" x="0" y="0" width="{_f(W)}" height="{_f(H)}"/>' + "".join(self.out) + "</svg>"
        )


def _wrap(text: str, width: int, max_rows: int) -> list[str]:
    """Word-wrap a battery-limit label into its box (the last row is shortened with … if needed)."""
    rows: list[str] = []
    for word in text.split():
        if rows and len(rows[-1]) + 1 + len(word) <= width:
            rows[-1] += f" {word}"
        else:
            rows.append(word)
    if len(rows) > max_rows:
        rows = rows[: max_rows - 1] + [" ".join(rows[max_rows - 1:])]
    return [r if len(r) <= width else r[: width - 1] + "…" for r in rows] or [""]


def _short(name: str) -> str:
    return name if len(name) <= 30 else name[:28] + "…"


def render_svg(
    model: EngineeringModel,
    standards: Standards,
    highlight: set[str] | None = None,
    revision: str = "A",
    status: str = "DRAFT — AI GENERATED FOR REVIEW",
) -> str:
    return _Renderer(model, standards, highlight or set()).render(revision, status)
