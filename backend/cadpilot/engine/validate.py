"""Deterministic validator. Runs BEFORE the human sees a draft.

Layers: structural, topology, engineering rules, data consistency. Every issue names the worker
agent that owns the fix, so the primary agent can route a replan.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ..agents.instrumentation import attachment_line, rule_applies
from ..engine import line_rules
from ..engine.layout import module_of_items
from ..engine.sizing import flow_m3h
from ..ingest.excel import SourceData
from ..model.engineering import EngineeringModel
from ..model.proposals import ValidationIssue, ValidationReport
from ..standards import Standards


class _Collector:
    def __init__(self) -> None:
        self.report = ValidationReport()

    def check(self, layer: str) -> None:
        self.report.checks_run[layer] = self.report.checks_run.get(layer, 0) + 1

    def add(self, layer, severity, code, message, refs=(), owner=None, rule=None) -> None:
        self.report.issues.append(
            ValidationIssue(
                layer=layer, severity=severity, code=code, message=message, refs=list(refs), owner=owner, rule=rule
            )
        )


def _structural(m: EngineeringModel, c: _Collector) -> None:
    L = "structural"
    tags = (
        [e.tag for e in m.equipment]
        + [n.tag for n in m.piping_nodes]
        + [ln.tag for ln in m.lines]
        + [i.tag for i in m.instruments]
        + [comp.tag for ln in m.lines for comp in ln.inline]
    )
    c.check(L)
    for tag, n in Counter(tags).items():
        if n > 1:
            c.add(L, "error", "DUPLICATE_TAG", f"Tag {tag} is used {n} times", [tag], "topology_agent")

    nodes = m.node_tags()
    inline_tags = {comp.tag for ln in m.lines for comp in ln.inline}
    instrument_tags = {i.tag for i in m.instruments}
    line_tags = {ln.tag for ln in m.lines}
    for ln in m.lines:
        c.check(L)
        for end in (ln.from_, ln.to):
            if end.item not in nodes:
                c.add(L, "error", "DANGLING_LINE", f"{ln.tag} references missing item {end.item}", [ln.tag], "topology_agent")
    for i in m.instruments:
        c.check(L)
        ref = i.attached_to.ref
        ok = ref in (line_tags if i.attached_to.kind == "line" else nodes)
        if not ok:
            c.add(L, "error", "BAD_REFERENCE", f"{i.tag} is attached to missing {i.attached_to.kind} {ref}", [i.tag], "instrumentation_agent")
        if i.loop and not any(lp.tag == i.loop for lp in m.loops):
            c.add(L, "error", "BAD_LOOP_REF", f"{i.tag} references missing loop {i.loop}", [i.tag], "instrumentation_agent")
    for lp in m.loops:
        c.check(L)
        if lp.measured_by not in instrument_tags:
            c.add(L, "error", "BAD_LOOP_REF", f"Loop {lp.tag}: measuring instrument {lp.measured_by} missing", [lp.tag], "instrumentation_agent")
        final_ok = lp.final_element in (nodes if lp.final_element_kind == "vfd" else instrument_tags | inline_tags)
        if not final_ok:
            c.add(L, "error", "BAD_LOOP_REF", f"Loop {lp.tag}: final element {lp.final_element} missing", [lp.tag], "instrumentation_agent")

    connected = {ln.from_.item for ln in m.lines} | {ln.to.item for ln in m.lines}
    for tag in nodes - connected:
        c.check(L)
        c.add(L, "error", "ORPHAN", f"{tag} is not connected to any line", [tag], "topology_agent")


def _topology(m: EngineeringModel, s: Standards, c: _Collector) -> None:
    L = "topology"
    kinds = {e.tag: e.type for e in m.equipment} | {n.tag: n.kind for n in m.piping_nodes}
    port_use: Counter = Counter()
    for ln in m.lines:
        for end in (ln.from_, ln.to):
            c.check(L)
            if kinds.get(end.item) in ("header", "tee"):
                continue
            valid = s.ports_for(kinds.get(end.item, ""))
            if end.port not in valid:
                c.add(L, "error", "INVALID_PORT", f"{ln.tag}: {end.item} has no port '{end.port}'", [ln.tag], "topology_agent")
            port_use[(end.item, end.port)] += 1
    for (item, port), n in port_use.items():
        if n > 1:
            c.add(L, "error", "PORT_REUSED", f"{item}.{port} is connected to {n} lines", [item], "topology_agent")

    for e in m.equipment:
        c.check(L)
        if not m.lines_into(e.tag):
            c.add(L, "error", "NO_INLET", f"{e.tag} ({e.name}) has no inlet line", [e.tag], "topology_agent")
        if not m.lines_out_of(e.tag):
            c.add(L, "error", "NO_OUTLET", f"{e.tag} ({e.name}) has no outlet line", [e.tag], "topology_agent")

    out_adj: dict[str, list[str]] = defaultdict(list)
    und: dict[str, set[str]] = defaultdict(set)
    for ln in m.lines:
        if ln.kind != "bypass":
            out_adj[ln.from_.item].append(ln.to.item)
        und[ln.from_.item].add(ln.to.item)
        und[ln.to.item].add(ln.from_.item)

    outlets = {n.tag for n in m.piping_nodes if n.kind == "terminal_out"}
    reached_outlets: set[str] = set()
    for n in m.piping_nodes:
        if n.kind != "terminal_in":
            continue
        c.check(L)
        seen, stack = set(), [n.tag]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack += out_adj[cur]
        reached_outlets |= seen & outlets
        if not seen & outlets:
            c.add(L, "error", "NO_PROCESS_PATH", f"No process path from {n.tag} to an outlet battery limit", [n.tag], "topology_agent")
    for tag in sorted(outlets - reached_outlets):
        c.check(L)
        c.add(L, "error", "NO_PROCESS_PATH", f"Outlet battery limit {tag} is not fed by any process path", [tag], "topology_agent")

    # Disconnected process islands — per engineering module (modules meet at battery limits)
    all_nodes = m.node_tags()
    module_of = module_of_items(m)
    for mod in dict.fromkeys(module_of.get(t, "") for t in sorted(all_nodes)):
        part = {t for t in all_nodes if module_of.get(t, "") == mod}
        c.check(L)
        start = next(iter(sorted(part)))
        seen, stack = set(), [start]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack += list(und[cur])
        rest = part - seen
        # a separate chain between its own battery limits (e.g. a whey line) is a route, not an island
        terminals = {n.tag for n in m.piping_nodes if n.kind in ("terminal_in", "terminal_out")}
        while rest:
            comp, stack = set(), [next(iter(sorted(rest)))]
            while stack:
                cur = stack.pop()
                if cur in comp:
                    continue
                comp.add(cur)
                stack += [x for x in und[cur] if x in part]
            if comp & terminals:
                rest -= comp
            else:
                break
        if rest:
            where = f" in module {mod}" if mod else ""
            c.add(L, "error", "ISLAND", f"{len(rest)} item(s) form a disconnected island{where}", sorted(rest), "topology_agent")

    # Directed cycles in the process path (bypasses excluded)
    c.check(L)
    state: dict[str, int] = {}
    path: list[str] = []
    cycle: list[str] = []

    def dfs(u: str) -> bool:
        state[u] = 1
        path.append(u)
        for v in out_adj[u]:
            if state.get(v) == 1:
                cycle.extend(path[path.index(v):] + [v])
                return True
            if state.get(v) is None and dfs(v):
                return True
        state[u] = 2
        path.pop()
        return False

    if any(state.get(t) is None and dfs(t) for t in sorted(all_nodes)):
        c.add(L, "error", "CYCLE", f"The process path contains a loop: {' → '.join(cycle)}", cycle[:-1], "topology_agent")


def _engineering(m: EngineeringModel, s: Standards, c: _Collector) -> None:
    L = "engineering"
    cfg = s.validation.get("engineering", {})
    waivers: dict[str, dict] = m.metadata.get("waivers", {})  # type: ignore[assignment]

    def waived(rule_id: str, function: str, ref: str | None) -> str | None:
        for tag, w in waivers.items():
            if w.get("rule") == rule_id and w.get("function") == function and (ref is None or w.get("ref") == ref):
                return tag
        return None

    # Required (non-optional) template stages present
    tpl = s.template(m.process.template)
    present = {st.id for st in m.process.stages}
    for st in tpl["stages"]:
        c.check(L)
        if not st.get("optional") and st["id"] not in present:
            c.add(L, "error", "MISSING_STAGE", f"Required stage '{st['name']}' is missing", [], "process_agent")

    order = [st.id for st in m.process.stages]
    for up, down, why in cfg.get("order_constraints", []):
        c.check(L)
        if up in order and down in order and order.index(up) > order.index(down):
            c.add(L, "warning", "ORDER_CONSTRAINT", f"'{up}' is after '{down}': {why}", [], "process_agent", "ORDER")

    if cfg.get("enforce_instrumentation_rules", True):
        for rule in s.instrumentation["rules"]:
            for eq in m.equipment:
                if not rule_applies(rule, eq):
                    continue
                line = attachment_line(rule["attach"], eq, m.lines)
                target = eq.tag if rule["attach"] == "equipment" else (line.tag if line else None)
                for spec in rule["instruments"]:
                    c.check(L)
                    found = any(
                        i.function == spec["function"] and i.attached_to.ref == target for i in m.instruments
                    )
                    if found:
                        continue
                    w = waived(rule["id"], spec["function"], target)
                    if w:
                        c.add(L, "info", "RULE_WAIVED", f"{spec['function']} on {target} waived by reviewer ({waivers[w]['reason']})", [eq.tag], None, rule["id"])
                    else:
                        c.add(
                            L, "error", "MISSING_INSTRUMENT",
                            f"{eq.tag}: {spec['function']} required on {rule['attach'].replace('_', ' ')} — {rule['rationale']}",
                            [eq.tag], "instrumentation_agent", rule["id"],
                        )
                if rule.get("loop"):
                    c.check(L)
                    ctrl = rule["loop"]["controller"]
                    has_loop = any(
                        lp.tag.startswith(f"{ctrl}-")
                        and (m.get_instrument(lp.measured_by) and m.get_instrument(lp.measured_by).attached_to.ref == target)  # type: ignore[union-attr]
                        for lp in m.loops
                    )
                    if not has_loop and not any(w.get("rule") == rule["id"] for w in waivers.values()):
                        c.add(L, "error", "MISSING_LOOP", f"{eq.tag}: {ctrl} control loop required — {rule['rationale']}", [eq.tag], "instrumentation_agent", rule["id"])

    if cfg.get("enforce_line_rules", True):
        kinds = {e.tag: e.type for e in m.equipment} | {n.tag: n.kind for n in m.piping_nodes}
        area = {e.tag: e.area for e in m.equipment} | {n.tag: n.area for n in m.piping_nodes}
        mod_of_area = line_rules.module_of_area(m.process)
        for vr in s.line_rules["valves"]:
            if vr["applies_to"] == "bypass":
                continue  # a reviewer's choice, not a requirement
            for ln in m.lines:
                if ln.kind == "bypass" or not line_rules.in_scope(vr, area.get(ln.from_.item) or area.get(ln.to.item), mod_of_area) \
                        or not line_rules.applies(vr["applies_to"], kinds.get(ln.from_.item), kinds.get(ln.to.item), ln.kind, ln.service):
                    continue
                c.check(L)
                if any(comp.type == vr["type"] and comp.provenance.rule == vr["id"] for comp in ln.inline):
                    continue
                if any(w.get("rule") == vr["id"] and w.get("ref") == ln.tag for w in waivers.values()):
                    c.add(L, "info", "RULE_WAIVED", f"{vr['type']} on {ln.tag} waived by reviewer", [ln.tag], None, vr["id"])
                else:
                    c.add(L, "error", "MISSING_VALVE", f"{ln.tag}: {vr['type'].replace('_', ' ')} required — {vr['rationale']}", [ln.tag], "topology_agent", vr["id"])


def _data(m: EngineeringModel, s: Standards, src: SourceData | None, c: _Collector) -> None:
    L = "data"
    cfg = s.validation.get("data", {})
    intake = m.process.basis.get("daily_intake_l")
    by_stage: dict[str, list] = defaultdict(list)
    for e in m.equipment:
        by_stage[e.stage].append(e)

    def total_flow(stage: str) -> float | None:
        items = by_stage.get(stage, [])
        flows = [flow_m3h(e.capacity) for e in items]
        return sum(f for f in flows if f) or None

    if isinstance(intake, (int, float)) and intake:
        intake_m3 = intake / 1000
        for stages, limit_key, label in (
            (("unloading_pump",), "reception_max_hours_per_day", "Reception"),
            (("pasteurizer", "pz_pasteurizer"), "processing_max_hours_per_day", "Pasteurization"),
        ):
            c.check(L)
            stage = next((x for x in stages if by_stage.get(x)), stages[0])
            f = total_flow(stage)
            if f and limit_key in cfg:
                hours = intake_m3 / f
                if hours > cfg[limit_key]:
                    c.add(L, "warning", "CAPACITY_HOURS", f"{label}: {intake_m3:.0f} m³/day at {f:.0f} m³/h needs {hours:.1f} h/day (limit {cfg[limit_key]} h)", [e.tag for e in by_stage[stage]], "equipment_agent")
        silos = by_stage.get("raw_milk_silo", [])
        vol = sum(e.capacity.value for e in silos if e.capacity and e.capacity.unit == "KL")
        c.check(L)
        need = cfg.get("raw_storage_min_fraction_of_daily_intake", 0) * intake_m3
        if silos and vol < need:
            c.add(L, "warning", "STORAGE_LOW", f"Raw milk storage {vol:.0f} KL is below {need:.0f} KL ({cfg['raw_storage_min_fraction_of_daily_intake']:.0%} of daily intake)", [e.tag for e in silos], "equipment_agent")

    for e in m.equipment:
        c.check(L)
        if e.capacity is None:
            if e.type not in cfg.get("capacity_optional_types", []):  # machines rated by batch, not flow
                c.add(L, "warning", "NO_CAPACITY", f"{e.tag} ({e.name}) has no capacity", [e.tag], "equipment_agent")
            continue
        cap = flow_m3h(e.capacity)
        if cap is None:
            continue
        for ln in m.lines_into(e.tag):
            line_flow = flow_m3h(ln.design_flow)  # same basis as the capacity (kg/h and t/h included)
            if line_flow is not None and ln.kind == "process" and cap + 1e-6 < line_flow:
                c.add(L, "warning", "UNDERSIZED", f"{e.tag} capacity {e.capacity} is below the design flow {ln.design_flow} of {ln.tag}", [e.tag, ln.tag], "equipment_agent")

    vmax = cfg.get("max_line_velocity_m_s")
    for ln in m.lines:
        c.check(L)
        if ln.size_dn is None:
            if ln.kind == "process" and line_rules.sized(s.line_rules, ln.service):
                c.add(L, "warning", "UNSIZED_LINE", f"{ln.tag} has no size", [ln.tag], "topology_agent")
            continue
        if ln.design_flow and vmax and (f := flow_m3h(ln.design_flow)) is not None:
            v, v_allowed = line_rules.line_velocity(s.line_rules, ln.service, f, ln.size_dn)
            if v > max(vmax, v_allowed):
                c.add(L, "warning", "HIGH_VELOCITY", f"{ln.tag}: {v:.2f} m/s at DN{ln.size_dn}", [ln.tag], "topology_agent")

    if src is not None and any(st.module for st in m.process.stages):
        _coverage(m, s, src, c)

    # a draft made while the workbooks still disagree on a design number is provisional
    modules = list(dict.fromkeys(st.module for st in m.process.stages if st.module)) or ["classic"]
    for p in m.metadata.get("basis", {}).get("parameters", []):
        relevant = "*" in p["modules"] or set(p["modules"]) & set(modules)
        if p["status"] == "conflict" and relevant:
            c.check(L)
            vals = "; ".join(f"{cd['source']} {cd['display']}" for cd in p["candidates"])
            c.add(L, "warning", "BASIS_CONFLICT", f"{p['name']}: the workbooks disagree ({vals}) and no value is approved — "
                  f"this draft used the mass balance / cost estimate. Approve the design basis before issuing.", [], "process_agent")

    # the mass balance decides how many units; say where the cost estimate disagrees
    for rec in m.metadata.get("sizing", []):
        c.check(L)
        if rec["units"] and rec["design_data_qty"] and rec["design_data_qty"] != rec["units"]:
            c.add(L, "warning", "QTY_DIFFERS_FROM_DESIGN_DATA",
                  f"{rec['stage_name']}: the mass balance needs {rec['units']} unit(s) ({rec['required']} ÷ {rec['unit_capacity']}"
                  f"{' + ' + str(rec['standby']) + ' standby' if rec['standby'] else ''}); the design data lists {rec['design_data_qty']} "
                  f"({rec['source']}). The drawing follows the mass balance.", [], "equipment_agent")

    if src is not None:
        rows = {r.ref: r for r in src.equipment_rows}
        for e in m.equipment:
            row = rows.get(e.provenance.source or "")
            if row is not None and row.flags:
                c.check(L)
                c.add(L, "warning", "SOURCE_ROW_UNREADABLE", f"{e.tag} comes from {row.ref.split(':')[-1]} where {'; '.join(row.flags)} — check the design data or correct it in chat", [e.tag], "equipment_agent")
            elif row is not None and row.read_by == "llm":
                c.check(L)
                c.add(L, "info", "SOURCE_READ_BY_AI", f"{e.tag}: a value in {row.ref.split(':')[-1]} was read by the AI ({row.note})", [e.tag], None)
            if row and row.capacity and e.capacity and (row.capacity.value, row.capacity.unit) != (e.capacity.value, e.capacity.unit):
                c.check(L)
                c.add(L, "info", "DIFFERS_FROM_SOURCE", f"{e.tag} capacity {e.capacity} differs from design data ({row.capacity})", [e.tag], None)


def _coverage(m: EngineeringModel, s: Standards, src: SourceData, c: _Collector) -> None:
    """Is the drawing the plant the workbooks describe? Every process row left out is named."""
    from ..agents.scope import coverage

    L = "data"
    modules = list(dict.fromkeys(st.module for st in m.process.stages if st.module))
    drawn = {e.provenance.source for e in m.equipment if e.provenance.source}
    cov = coverage(src, s, modules, drawn)

    def rows(ev) -> str:
        head = ", ".join(f"{e.ref.split('!')[-1]} {e.text[:40]}" for e in ev[:6])
        return head + (f" (+{len(ev) - 6} more)" if len(ev) > 6 else "")

    for section, ev in cov.not_drawn.items():
        c.check(L)
        c.add(L, "warning", "ROWS_NOT_DRAWN", f"{section.title()}: {len(ev)} equipment row(s) of the design data are not on "
              f"the drawing — {rows(ev)}", [], "equipment_agent")
    for section, ev in cov.uncovered.items():
        c.check(L)
        c.add(L, "warning", "SECTION_NOT_COVERED", f"{section.title()} ({len(ev)} equipment rows) is not covered by any "
              f"engineering module, so it is not drawn — {rows(ev)}", [], "process_agent")
    for product in cov.products_uncovered:
        c.check(L)
        c.add(L, "warning", "PRODUCT_NOT_COVERED", f"Mass balance product {product} is made by no engineering module — "
              f"it is not on this drawing", [], "process_agent")
    for product in cov.products_deselected:
        c.check(L)
        c.add(L, "info", "PRODUCT_NOT_SELECTED", f"Mass balance product {product}", [], None)
    m.metadata["coverage"] = {"rows_total": cov.rows_total, "rows_drawn": cov.rows_drawn,
                              "sections_not_covered": list(cov.uncovered), "products_not_covered": cov.products_uncovered}


def validate(model: EngineeringModel, standards: Standards, source: SourceData | None = None) -> ValidationReport:
    c = _Collector()
    _structural(model, c)
    _topology(model, standards, c)
    _engineering(model, standards, c)
    _data(model, standards, source, c)
    return c.report
