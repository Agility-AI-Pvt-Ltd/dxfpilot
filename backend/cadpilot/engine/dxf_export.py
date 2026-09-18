"""DXF export of a rendered P&ID.

The DXF is produced from the same SVG the reviewer sees, so both always agree. Geometry is
converted to millimetres (Y axis flipped: DXF Y points up) and sorted onto CAD layers by what each
object is (equipment, piping, valves, instruments, control signals, areas, title block).

Only the SVG subset the renderer emits is handled: g[transform=translate/rotate], path (M/L/H/V/Q/Z,
absolute and relative), line, rect, circle, text.
"""

from __future__ import annotations

import io
import math
import re
import xml.etree.ElementTree as ET

import ezdxf
from ezdxf.enums import TextEntityAlignment

MM_PER_UNIT = 0.25  # drawing units are ~px; 0.25 mm each puts the sheet around A1/A0 width
SVG_NS = "{http://www.w3.org/2000/svg}"

# layer: (ACI colour, lineweight in 1/100 mm, linetype)
LAYERS = {
    "PID-EQUIPMENT": (7, 35, "Continuous"),
    "PID-PIPING": (7, 35, "Continuous"),
    "PID-PIPING-BYPASS": (7, 35, "DASHED"),
    "PID-HEADERS": (7, 70, "Continuous"),
    "PID-VALVES": (7, 25, "Continuous"),
    "PID-INSTRUMENTS": (4, 18, "Continuous"),
    "PID-SIGNALS": (5, 13, "DASHED2"),
    "PID-NODES": (7, 25, "Continuous"),
    "PID-AREAS": (8, 13, "DASHED"),
    "PID-TEXT": (7, 0, "Continuous"),
    "PID-TITLE": (7, 25, "Continuous"),
}

KIND_LAYER = {
    "equipment": "PID-EQUIPMENT",
    "line": "PID-PIPING",
    "inline": "PID-VALVES",
    "instrument": "PID-INSTRUMENTS",
    "piping_node": "PID-NODES",
}

# text height (drawing units) per CSS class used by the renderer
TEXT_HEIGHT = {
    "tag": 12, "sub": 9.5, "ln-label": 9, "fn": 9.5, "loopno": 8.5, "alarm": 8,
    "area-label": 11, "tb-t": 11, "tb-b": 14, "sym-note": 9,
}

Matrix = tuple[float, float, float, float, float, float]  # a b c d e f (SVG affine)
IDENTITY: Matrix = (1, 0, 0, 1, 0, 0)


def _mul(m: Matrix, n: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2, a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def _parse_transform(t: str | None) -> Matrix:
    m = IDENTITY
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", t or ""):
        v = [float(x) for x in re.split(r"[\s,]+", args.strip()) if x]
        if name == "translate":
            m = _mul(m, (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0))
        elif name == "rotate":
            r = math.radians(v[0])
            m = _mul(m, (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0))
        elif name == "scale":
            m = _mul(m, (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0))
    return m


def _path_subpaths(d: str) -> list[tuple[list[tuple[float, float]], bool]]:
    """SVG path data → list of (points, closed). Quadratic curves are flattened."""
    tokens = re.findall(r"[MmLlHhVvQqZz]|-?\d*\.?\d+(?:e-?\d+)?", d)
    out: list[tuple[list[tuple[float, float]], bool]] = []
    pts: list[tuple[float, float]] = []
    x = y = sx = sy = 0.0
    i, cmd = 0, ""

    def num() -> float:
        nonlocal i
        v = float(tokens[i])
        i += 1
        return v

    while i < len(tokens):
        if re.fullmatch(r"[A-Za-z]", tokens[i]):
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                if pts:
                    out.append((pts, True))
                pts = []
                x, y = sx, sy
                continue
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            if pts:
                out.append((pts, False))
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            sx, sy = x, y
            pts = [(x, y)]
            cmd = "l" if rel else "L"  # further pairs are implicit lineto
        elif c == "L":
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            pts.append((x, y))
        elif c == "H":
            nx = num()
            x = x + nx if rel else nx
            pts.append((x, y))
        elif c == "V":
            ny = num()
            y = y + ny if rel else ny
            pts.append((x, y))
        elif c == "Q":
            cx, cy, nx, ny = num(), num(), num(), num()
            if rel:
                cx, cy, nx, ny = x + cx, y + cy, x + nx, y + ny
            for k in range(1, 9):
                t = k / 8
                pts.append(((1 - t) ** 2 * x + 2 * (1 - t) * t * cx + t * t * nx, (1 - t) ** 2 * y + 2 * (1 - t) * t * cy + t * t * ny))
            x, y = nx, ny
        else:  # unsupported command: skip its token
            i += 1
    if pts:
        out.append((pts, False))
    return out


class _Writer:
    def __init__(self, height: float):
        self.doc = ezdxf.new("R2018", setup=True)
        self.doc.header["$INSUNITS"] = 4  # millimetres
        self.doc.header["$LTSCALE"] = 2.0
        for name, (color, lw, lt) in LAYERS.items():
            layer = self.doc.layers.add(name, color=color, linetype=lt)
            layer.dxf.lineweight = lw
        self.msp = self.doc.modelspace()
        self.height = height

    def pt(self, m: Matrix, x: float, y: float) -> tuple[float, float]:
        a, b, c, d, e, f = m
        px, py = a * x + c * y + e, b * x + d * y + f
        return round(px * MM_PER_UNIT, 3), round((self.height - py) * MM_PER_UNIT, 3)

    def poly(self, pts: list[tuple[float, float]], closed: bool, layer: str, fill: bool = False) -> None:
        if len(pts) < 2:
            return
        if fill and closed and len(pts) >= 3:
            hatch = self.msp.add_hatch(dxfattribs={"layer": layer})
            hatch.paths.add_polyline_path(pts, is_closed=True)
        self.msp.add_lwpolyline(pts, close=closed, dxfattribs={"layer": layer})


def _layer_for(el: ET.Element, inherited: str) -> str:
    cls = (el.get("class") or "").split()
    if "signal" in cls:
        return "PID-SIGNALS"
    if "leader" in cls:
        return "PID-INSTRUMENTS"
    if "area" in cls or "area-label" in cls:
        return "PID-AREAS"
    if "header" in cls:
        return "PID-HEADERS"
    if "pipe" in cls and "bypass" in cls:
        return "PID-PIPING-BYPASS"
    return inherited


SKIP_CLASSES = {"pipe-hit", "sheet", "cloud"}


def svg_to_dxf(svg: str) -> bytes:
    root = ET.fromstring(svg)
    vb = [float(v) for v in (root.get("viewBox") or "0 0 0 0").split()]
    w = _Writer(height=vb[3])

    def walk(el: ET.Element, m: Matrix, layer: str) -> None:
        tag = el.tag.replace(SVG_NS, "")
        cls = set((el.get("class") or "").split())
        if tag == "style" or cls & SKIP_CLASSES:
            return
        m = _mul(m, _parse_transform(el.get("transform")))
        kind = el.get("data-kind")
        if kind:
            layer = KIND_LAYER.get(kind, layer)
        if "title-block" in cls:
            layer = "PID-TITLE"
        lyr = _layer_for(el, layer)
        fill = "sym-fill" in cls or "arrow" in cls

        if tag == "path":
            for pts, closed in _path_subpaths(el.get("d", "")):
                w.poly([w.pt(m, x, y) for x, y in pts], closed, lyr, fill=fill)
        elif tag == "line":
            x1, y1, x2, y2 = (float(el.get(k, 0)) for k in ("x1", "y1", "x2", "y2"))
            w.msp.add_line(w.pt(m, x1, y1), w.pt(m, x2, y2), dxfattribs={"layer": lyr})
        elif tag == "rect":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            rw, rh = float(el.get("width", 0)), float(el.get("height", 0))
            w.poly([w.pt(m, x, y), w.pt(m, x + rw, y), w.pt(m, x + rw, y + rh), w.pt(m, x, y + rh)], True, lyr, fill=fill)
        elif tag == "circle":
            cx, cy, r = float(el.get("cx", 0)), float(el.get("cy", 0)), float(el.get("r", 0))
            scale = math.hypot(m[0], m[1])
            if fill:
                hatch = w.msp.add_hatch(dxfattribs={"layer": lyr})
                hatch.paths.add_edge_path().add_arc(w.pt(m, cx, cy), r * scale * MM_PER_UNIT, 0, 360)
            w.msp.add_circle(w.pt(m, cx, cy), r * scale * MM_PER_UNIT, dxfattribs={"layer": lyr})
        elif tag == "text":
            text = "".join(el.itertext()).strip()
            if text:
                size = next((TEXT_HEIGHT[c] for c in cls if c in TEXT_HEIGHT), 10)
                if (fs := re.search(r"font-size:\s*([\d.]+)", el.get("style", ""))):
                    size = float(fs.group(1))
                anchor = el.get("text-anchor", "start")
                align = {"middle": TextEntityAlignment.BOTTOM_CENTER, "end": TextEntityAlignment.BOTTOM_RIGHT}.get(
                    anchor, TextEntityAlignment.BOTTOM_LEFT
                )
                x, y = float(el.get("x", 0)), float(el.get("y", 0))
                text_layer = lyr if lyr in ("PID-TITLE", "PID-AREAS") else "PID-TEXT"
                w.msp.add_text(text, height=size * 0.72 * MM_PER_UNIT, dxfattribs={"layer": text_layer}).set_placement(
                    w.pt(m, x, y), align=align
                )
        for child in el:
            walk(child, m, layer)

    walk(root, IDENTITY, "PID-EQUIPMENT")
    buf = io.StringIO()
    w.doc.write(buf)
    return buf.getvalue().encode("utf-8")
