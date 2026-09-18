"""Deterministic layout: the same model always produces the same drawing.

Process flows left→right. Columns come from longest-path layering of the process graph;
parallel trains stack vertically by train index; headers sit in their own narrow column.
"""

from __future__ import annotations

from collections import defaultdict

from ..model.engineering import EngineeringModel, Layout, LayoutPoint
from ..model.intent import DesignIntent
from ..standards import Standards


def layout(model: EngineeringModel, standards: Standards, intent: DesignIntent | None = None) -> Layout:
    cfg = standards.layout
    kinds = {e.tag: e.type for e in model.equipment} | {n.tag: n.kind for n in model.piping_nodes}
    trains = {e.tag: e.train for e in model.equipment} | {n.tag: n.train for n in model.piping_nodes}
    group_of_stage = {s.id: s.group for s in model.process.stages}
    stage_of = {e.tag: e.stage for e in model.equipment} | {n.tag: n.stage for n in model.piping_nodes}

    succ: dict[str, list[str]] = defaultdict(list)
    indeg: dict[str, int] = {t: 0 for t in kinds}
    for ln in model.lines:
        if ln.kind == "bypass":
            continue
        succ[ln.from_.item].append(ln.to.item)
        indeg[ln.to.item] = indeg.get(ln.to.item, 0) + 1

    # Longest-path layering (Kahn)
    layer: dict[str, int] = {t: 0 for t in kinds}
    queue = sorted(t for t, d in indeg.items() if d == 0)
    remaining = dict(indeg)
    while queue:
        u = queue.pop(0)
        for v in succ[u]:
            layer[v] = max(layer[v], layer[u] + 1)
            remaining[v] -= 1
            if remaining[v] == 0:
                queue.append(v)
    # Keep outlet battery limits in the last column
    last = max(layer.values(), default=0)
    for t, k in kinds.items():
        if k == "terminal_out":
            layer[t] = last

    # Column spacing is driven by content: each gap must fit the line label plus every inline
    # component (valves, flow elements) on the lines leaving that column.
    def half_width(t: str) -> float:
        k = kinds[t]
        if k in ("header", "tee"):
            return 4
        sym = standards.symbols["equipment"].get(k) or standards.symbols["piping_nodes"].get(k)
        extra = cfg["instrument_side_width"] if k == "silo" else 0
        return sym["size"][0] / 2 + extra if sym else 20

    halves: dict[int, float] = defaultdict(float)
    for t, lyr in layer.items():
        halves[lyr] = max(halves[lyr], half_width(t))
    gaps: dict[int, float] = defaultdict(lambda: cfg["min_gap"])
    bubbles_on: dict[str, int] = defaultdict(int)
    for inst in model.instruments:
        if inst.attached_to.kind == "line":
            bubbles_on[inst.attached_to.ref] += 1
    for ln in model.lines:
        if ln.kind == "bypass":
            continue
        need = (
            cfg["line_label_width"]
            + cfg["inline_pitch"] * len(ln.inline)
            + cfg["instrument_spacing_x"] * bubbles_on[ln.tag]
        )
        lyr = layer[ln.from_.item]
        gaps[lyr] = max(gaps[lyr], need)
    xs: dict[int, float] = {}
    x = float(cfg["margin"])
    layers_sorted = sorted(halves)
    for i, lyr in enumerate(layers_sorted):
        x += halves[lyr]
        xs[lyr] = x
        x += halves[lyr] + (gaps[lyr] if i < len(layers_sorted) - 1 else 0)

    group_size: dict[str, int] = model.process.groups
    max_n = max(group_size.values(), default=1)
    top = cfg["margin"] + cfg["instrument_offset_y"] + 60
    mid = top + (max_n - 1) / 2 * cfg["row_height"]

    pos: dict[str, LayoutPoint] = {}
    for t, k in kinds.items():
        if k in ("header", "tee"):
            continue
        g = group_of_stage.get(stage_of.get(t) or "", "")
        n = group_size.get(g, 1)
        tr = (trains.get(t) or 1) - 1
        y = mid + (tr - (n - 1) / 2) * cfg["row_height"]
        pos[t] = LayoutPoint(x=xs[layer[t]], y=y)

    for n in model.piping_nodes:
        if n.kind == "header":
            pos[n.tag] = LayoutPoint(x=xs[layer[n.tag]], y=mid)
    # Tees align with the item they sit next to
    for n in model.piping_nodes:
        if n.kind != "tee":
            continue
        nbrs = [ln.to.item for ln in model.lines if ln.from_.item == n.tag and ln.kind != "bypass"] + [
            ln.from_.item for ln in model.lines if ln.to.item == n.tag and ln.kind != "bypass"
        ]
        ys = [pos[t].y for t in nbrs if t in pos]
        pos[n.tag] = LayoutPoint(x=xs[layer[n.tag]], y=ys[0] if ys else mid)

    if intent:
        for tag, (dx, dy) in intent.layout_offsets.items():
            if tag in pos:
                pos[tag] = LayoutPoint(x=pos[tag].x + dx, y=pos[tag].y + dy)

    width = max(x + cfg["margin"], cfg["sheet_min_width"])
    height = mid + (max_n - 1) / 2 * cfg["row_height"] + cfg["row_height"] / 2 + cfg["margin"] + cfg["title_block_height"]
    return Layout(positions=pos, width=width, height=height)
