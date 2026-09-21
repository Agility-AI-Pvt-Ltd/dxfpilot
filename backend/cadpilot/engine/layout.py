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
    if any(st.module for st in model.process.stages):
        return _layout_modules(model, standards, intent)
    return _layout_single(model, standards, intent)


def module_of_items(model: EngineeringModel) -> dict[str, str]:
    """Every drawn item → its module (headers and tees take the module of what they connect)."""
    stage_module = {st.id: st.module or "" for st in model.process.stages}
    out = {e.tag: stage_module.get(e.stage, "") for e in model.equipment}
    out |= {n.tag: stage_module.get(n.stage or "", "") for n in model.piping_nodes if n.stage}
    changed = True
    while changed:  # headers next to tees next to headers, however long the chain
        changed = False
        for ln in model.lines:
            a, b = ln.from_.item, ln.to.item
            if a in out and b not in out:
                out[b], changed = out[a], True
            elif b in out and a not in out:
                out[a], changed = out[b], True
    module_of_area = {st.area: st.module or "" for st in model.process.stages}
    for n in model.piping_nodes:  # connected to nothing with a module: its area (one per module) says which
        out.setdefault(n.tag, module_of_area.get(n.area, next((st.module or "" for st in model.process.stages), "")))
    return out


def _layout_modules(model: EngineeringModel, standards: Standards, intent: DesignIntent | None) -> Layout:
    """One horizontal band per engineering module, stacked top to bottom in module order; a module's
    side branches (dosing, condensate return, ...) get their own rows under its main chain."""
    cfg = standards.layout
    module_of = module_of_items(model)
    order = list(dict.fromkeys(st.module or "" for st in model.process.stages))
    branch_of = {st.id: st.branch for st in model.process.stages}
    stage_of = {e.tag: e.stage for e in model.equipment} | {n.tag: n.stage for n in model.piping_nodes}
    pos: dict[str, LayoutPoint] = {}
    top_pad = cfg["instrument_offset_y"] + 60
    y_cursor = float(cfg["margin"])
    width = float(cfg["sheet_min_width"])
    for mod in order:
        items = {t for t, m in module_of.items() if m == mod}
        stages = {st.id for st in model.process.stages if (st.module or "") == mod}
        sub = model.model_copy(update={
            "equipment": [e for e in model.equipment if e.tag in items],
            "piping_nodes": [n for n in model.piping_nodes if n.tag in items],
            "lines": [ln for ln in model.lines if ln.from_.item in items and ln.to.item in items],
            "instruments": [i for i in model.instruments if i.attached_to.ref in items
                            or any(ln.tag == i.attached_to.ref and ln.from_.item in items for ln in model.lines)],
            "process": model.process.model_copy(update={
                "stages": [st for st in model.process.stages if st.id in stages],
                "groups": {g: n for g, n in model.process.groups.items()
                           if any(st.group == g and st.id in stages for st in model.process.stages)},
            }),
        })
        band = _layout_single(sub, standards, None).positions
        if not band:
            continue
        # branches: below the main chain, one row per train
        main_ys = [p.y for t, p in band.items() if not branch_of.get(stage_of.get(t) or "")]
        row_y = (max(main_ys) if main_ys else 0) + cfg["row_height"]
        for br in dict.fromkeys(branch_of[st] for st in stages if branch_of.get(st)):
            br_items = [t for t in band if branch_of.get(stage_of.get(t) or "") == br]
            trains = sorted({(model.get_equipment(t).train if model.get_equipment(t) else None) or
                             next((n.train for n in model.piping_nodes if n.tag == t), 1) or 1 for t in br_items})
            for t in br_items:
                tr = (model.get_equipment(t).train if model.get_equipment(t) else None) or \
                    next((n.train for n in model.piping_nodes if n.tag == t), 1) or 1
                band[t] = LayoutPoint(x=band[t].x, y=row_y + trains.index(tr) * cfg["row_height"])
            row_y += len(trains) * cfg["row_height"]
        top = min(p.y for p in band.values())
        shift = y_cursor + top_pad - top
        for t, p in band.items():
            pos[t] = LayoutPoint(x=p.x, y=p.y + shift)
        y_cursor = max(p.y for p in band.values()) + shift + cfg["row_height"] * 0.75
        width = max(width, max(p.x for p in band.values()) + 2 * cfg["margin"] + 60)

    if intent:
        for tag, (dx, dy) in intent.layout_offsets.items():
            if tag in pos:
                pos[tag] = LayoutPoint(x=pos[tag].x + dx, y=pos[tag].y + dy)
    return Layout(positions=pos, width=width, height=y_cursor + cfg["margin"] + cfg["title_block_height"])


def _layout_single(model: EngineeringModel, standards: Standards, intent: DesignIntent | None = None) -> Layout:
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
