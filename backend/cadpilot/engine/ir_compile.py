"""Deterministic compiler: LLM P&ID plan (IR) → topology + instrumentation proposals.

The compiler never repairs the plan. Anything it cannot resolve — an unknown tag, node or rule id,
a rule used for the wrong kind of valve, a loop without its measurement — becomes an `IR_*`
validation error that is sent back to the planner. Tags, line numbers, flows, sizes and services
are always computed here, from the same registry and flow code as the rules engine.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..agents.context import AgentContext
from ..agents.instrumentation import LINE_VALVE_FINAL, _walk_to_pump, attachment_line, rule_applies
from ..model.engineering import (
    ControlLoop,
    Equipment,
    InlineComponent,
    Instrument,
    InstrumentAttachment,
    Line,
    PipingNode,
    PortRef,
    ProcessDefinition,
    Provenance,
    Quantity,
)
from ..model.ir import PIDPlan
from ..model.proposals import InstrumentationProposal, TopologyProposal, ValidationIssue
from .flows import LineFlows
from . import line_rules

AGENT = "llm_planner"


@dataclass
class Compiled:
    topology: TopologyProposal
    instrumentation: InstrumentationProposal
    errors: list[ValidationIssue] = field(default_factory=list)
    connection_of_line: dict[str, str] = field(default_factory=dict)  # line tag -> "SOURCE>TARGET" as in the IR
    node_of_tag: dict[str, str] = field(default_factory=dict)  # piping node tag -> the planner's node id


def _err(code: str, message: str, refs: list[str] | None = None) -> ValidationIssue:
    return ValidationIssue(layer="structural", severity="error", code=code, message=message, refs=refs or [], owner=AGENT)


def compile_plan(plan: PIDPlan, ctx: AgentContext, process: ProcessDefinition, equipment: list[Equipment]) -> Compiled:
    tagger = ctx.tagger
    errors: list[ValidationIssue] = []
    std = ctx.standards
    stage_by_id = {s.id: s for s in process.stages}
    eq_by_tag = {e.tag: e for e in equipment}

    kinds: dict[str, str] = {e.tag: e.type for e in equipment}
    group_of: dict[str, str] = {e.tag: stage_by_id[e.stage].group for e in equipment if e.stage in stage_by_id}
    train_of: dict[str, int | None] = {e.tag: e.train for e in equipment}
    area_of: dict[str, str] = {e.tag: e.area for e in equipment}

    # ---- nodes (tags assigned after connections tell us where they sit) ---------------------------
    node_decl = {}
    for n in plan.nodes:
        if n.id in eq_by_tag or n.id in node_decl:
            errors.append(_err("IR_DUPLICATE_ID", f"Node id '{n.id}' is used twice or clashes with an equipment tag"))
            continue
        node_decl[n.id] = n

    def resolve(ref: str) -> str | None:
        ref = ref.strip()
        return ref if ref in eq_by_tag or ref in node_decl else None

    # ---- connections ---------------------------------------------------------------------------
    edges: list[tuple[str, str, str]] = []  # (source id, target id, kind)
    for c in plan.connections:
        s, t = resolve(c.source), resolve(c.target)
        if s is None or t is None:
            bad = c.source if s is None else c.target
            errors.append(_err("IR_UNKNOWN_ITEM", f"Connection {c.source}>{c.target}: '{bad}' is neither an equipment tag nor a declared node"))
            continue
        if s == t or any(e[0] == s and e[1] == t for e in edges):
            errors.append(_err("IR_BAD_CONNECTION", f"Connection {c.source}>{c.target} is a duplicate or connects an item to itself"))
            continue
        edges.append((s, t, c.kind))

    # stages with a different number of units meet at a header (template convention), never unit-to-unit
    stage_size: dict[str, int] = defaultdict(int)
    for e in equipment:
        stage_size[e.stage] += 1
    def fed_size(src_stage: str, dst_stage: str) -> int:
        """A side chain that feeds some trains of a stage (e.g. dosing into the lye and acid tanks) meets that subset."""
        st = stage_by_id.get(src_stage)
        if st is not None and st.feeds == dst_stage and st.feeds_trains:
            return len(st.feeds_trains)
        return stage_size[dst_stage]

    for s_, t_, k in edges:
        gs, gt = group_of.get(s_), group_of.get(t_)
        if (k == "process" and s_ in eq_by_tag and t_ in eq_by_tag and eq_by_tag[s_].stage != eq_by_tag[t_].stage
                and stage_size[eq_by_tag[s_].stage] != fed_size(eq_by_tag[s_].stage, eq_by_tag[t_].stage)):
            ups = sorted(e.tag for e in equipment if group_of.get(e.tag) == gs and eq_by_tag[s_].stage == e.stage)
            downs = sorted(e.tag for e in equipment if group_of.get(e.tag) == gt and eq_by_tag[t_].stage == e.stage)
            errors.append(_err("IR_GROUP_ROUTING", f"Connection {s_}>{t_} links {len(ups)} unit(s) ({', '.join(ups)}) directly to "
                                                   f"{len(downs)} unit(s) ({', '.join(downs)}); different unit counts meet at one header — "
                                                   f"remove the direct connections and route each of {', '.join(ups)} → a new header → each of "
                                                   f"{', '.join(downs)}", [s_, t_]))

    neighbours: dict[str, list[str]] = defaultdict(list)
    for s, t, _ in edges:
        neighbours[s].append(t)
        neighbours[t].append(s)

    node_tag: dict[str, str] = {}
    nodes: list[PipingNode] = []
    template_label = {st["kind"]: st.get("label", "") for st in ctx.template["stages"] if st["kind"].startswith("terminal")}
    for nid, n in node_decl.items():
        near = _nearest_equipment(nid, neighbours, eq_by_tag)
        area = area_of.get(near, "1") if near else "1"
        tag = tagger.piping_node(f"ir:{n.kind}:{nid}", n.kind, area)
        node_tag[nid] = tag
        kinds[tag] = n.kind
        area_of[tag] = area
        if near:  # terminals and tees belong to the train they serve (for flows and layout)
            group_of[tag] = group_of.get(near, "")
            train_of[tag] = train_of.get(near)
        nodes.append(PipingNode(
            tag=tag, kind=n.kind, label=n.label or template_label.get(n.kind, ""), area=area,
            stage=_terminal_stage(n.kind, near, eq_by_tag, process) if n.kind.startswith("terminal") else None,
            train=train_of.get(tag),
            provenance=Provenance(agent=AGENT, rationale=f"Planned node '{nid}'"),
        ))

    def tag(x: str) -> str:
        return node_tag.get(x, x)

    # ---- services (what flows in each pipe) --------------------------------------------------------
    nodes_by_tag = {n.tag: n for n in nodes}
    inlet_service = next((st.outlet_service for st in process.stages if st.kind == "terminal_in" and st.outlet_service), "RM")
    succ: dict[str, list[str]] = defaultdict(list)
    indeg: dict[str, int] = defaultdict(int)
    for s, t, k in edges:
        if k == "process":
            succ[s].append(t)
            indeg[t] += 1
    service_out: dict[str, str] = {}
    queue = [x for x in set(neighbours) if indeg[x] == 0]
    seen: set[str] = set()
    while queue:
        x = queue.pop(0)
        if x in seen:
            continue
        seen.add(x)
        if x not in service_out:
            # a battery limit carries the service of the template stage it stands for (culture, steam, ...)
            node = nodes_by_tag.get(tag(x))
            st = stage_by_id.get(node.stage) if node is not None and node.stage else None
            service_out[x] = (st.outlet_service if st is not None and st.outlet_service else inlet_service)
        eq = eq_by_tag.get(x)
        out = stage_by_id[eq.stage].outlet_service if eq and eq.stage in stage_by_id and stage_by_id[eq.stage].outlet_service else service_out[x]
        service_out[x] = out
        for y in succ[x]:
            service_out.setdefault(y, out)
            indeg[y] -= 1
            if indeg[y] <= 0:
                queue.append(y)

    # ---- lines ---------------------------------------------------------------------------------
    rules = std.line_rules
    flows = LineFlows([(tag(s), tag(t)) for s, t, k in edges if k == "process"], kinds, group_of, train_of, equipment, ctx.template)
    ports: dict[str, int] = defaultdict(int)

    def port(item: str, side: str, bypass: bool) -> str:
        if kinds.get(item) in ("header", "tee"):
            if bypass:
                return "branch"
            ports[f"{item}:{side}"] += 1
            return f"{side}{ports[f'{item}:{side}']}"
        # a second connection on the same side uses the auxiliary port (side feed, cream outlet)
        ports[f"{item}:{side}"] += 1
        main, aux = ("outlet", "aux_out") if side == "out" else ("inlet", "aux_in")
        has_aux = aux in std.ports_for(kinds.get(item, ""))
        return aux if ports[f"{item}:{side}"] > 1 and has_aux else main

    lines: list[Line] = []
    line_of: dict[str, Line] = {}
    connection_of_line: dict[str, str] = {}
    second_outlet_service = {st.source: st.source_service for st in process.stages if st.source and st.source_service}
    for s, t, k in edges:
        u, d = tag(s), tag(t)
        svc = service_out.get(s, inlet_service)
        from_port = port(u, "out", k == "bypass")
        if from_port == "aux_out" and s in eq_by_tag:  # a second outlet carries its own product (cream, serum)
            svc = second_outlet_service.get(eq_by_tag[s].stage, svc)
        area = area_of.get(u) or area_of.get(d) or "1"
        f = flows.line_flow(u, d)
        line = Line(
            tag=tagger.line(f"{u}>{d}", svc, area),
            **{"from": PortRef(item=u, port=from_port)},
            to=PortRef(item=d, port=port(d, "in", k == "bypass")),
            service=svc, stream=svc, kind=k,  # type: ignore[arg-type]
            design_flow=Quantity(**dict(zip(("value", "unit"), line_rules.flow_quantity(rules, svc, f)))) if f else None,
            size_dn=line_rules.size(rules, svc, f) if f else None,
            spec=rules["spec"].get("by_service", {}).get(svc, rules["spec"]["default"]),
            provenance=Provenance(agent=AGENT, rule="LR-BYPASS" if k == "bypass" else "TOPO-SEQUENCE", rationale=f"Planned connection {s} → {t}"),
        )
        lines.append(line)
        line_of[f"{s}>{t}"] = line
        connection_of_line[line.tag] = f"{s}>{t}"

    def find_line(ref: str) -> Line | None:
        return line_of.get(ref.replace(" ", ""))

    # ---- valves (from line rules only) ---------------------------------------------------------------
    valve_rules = {v["id"]: v for v in rules["valves"]}
    inline: dict[str, list[InlineComponent]] = defaultdict(list)
    seen_valves: set[tuple[str, str, str]] = set()
    for v in plan.valves:
        key = (v.on.replace(" ", ""), v.type, v.rule)
        if key in seen_valves:
            errors.append(_err("IR_DUPLICATE_ENTRY", f"Valve {v.type} ({v.rule}) on {v.on} is listed twice — keep one"))
            continue
        seen_valves.add(key)
        rule = valve_rules.get(v.rule)
        line = find_line(v.on)
        if rule is None:
            errors.append(_err("IR_UNKNOWN_RULE", f"Valve on {v.on}: '{v.rule}' is not a line rule ({', '.join(valve_rules)})"))
            continue
        if line is None:
            errors.append(_err("IR_UNKNOWN_CONNECTION", f"Valve {v.rule}: connection '{v.on}' is not in the plan"))
            continue
        if rule["type"] != v.type:
            errors.append(_err("IR_RULE_MISMATCH", f"Valve on {v.on}: rule {v.rule} requires a {rule['type']}, not a {v.type}"))
            continue
        if not line_rules.in_scope(rule, area_of.get(line.from_.item) or area_of.get(line.to.item), line_rules.module_of_area(process)) \
                or not line_rules.applies(rule["applies_to"], kinds.get(line.from_.item), kinds.get(line.to.item), line.kind, line.service):
            errors.append(_err("IR_RULE_NOT_APPLICABLE", f"Valve on {v.on}: rule {v.rule} applies to {line_rules.describe(rule['applies_to'])} "
                                                         f"only; this connection is not one — remove the valve"))
            continue
        owner = line_rules.owner(rule["applies_to"], line.from_.item, line.to.item)
        inline[line.tag].append(InlineComponent(
            tag=tagger.valve(f"{rule['id']}:{owner}", rule["class"], area_of.get(line.from_.item, "1")),
            type=rule["type"], position=rule["position"],
            provenance=Provenance(agent=AGENT, rule=rule["id"], rationale=rule["rationale"]),
        ))

    # ---- instruments (from instrumentation rules only) -------------------------------------------------
    cfg = std.instrumentation
    symbols = cfg["symbols"]
    inst_rules = {r["id"]: r for r in cfg["rules"]}
    instruments: list[Instrument] = []
    by_place: dict[tuple[str, str], Instrument] = {}  # (function, on) -> instrument
    planned_as: dict[str, str] = {}  # instrument tag -> the 'on' it was planned with
    loop_controllers = {r["loop"]["controller"]: r["id"] for r in cfg["rules"] if r.get("loop")}

    for spec in plan.instruments:
        if (spec.function, spec.on.replace(" ", "")) in by_place:
            errors.append(_err("IR_DUPLICATE_ENTRY", f"Instrument {spec.function} on {spec.on} is listed twice — keep one"))
            continue
        rule = inst_rules.get(spec.rule)
        if rule is None:
            errors.append(_err("IR_UNKNOWN_RULE", f"Instrument {spec.function} on {spec.on}: '{spec.rule}' is not an instrumentation rule"))
            continue
        if spec.function in loop_controllers and not any(i["function"] == spec.function for i in rule["instruments"]):
            errors.append(_err("IR_RULE_MISMATCH", f"{spec.function} on {spec.on} is the controller of a loop, not an instrument — remove it; "
                                                   f"the loop entry {{rule, equipment}} creates the controller"))
            continue
        line = find_line(spec.on) if ">" in spec.on else None
        target_eq = eq_by_tag.get(spec.on.strip())
        if line is None and target_eq is None:
            errors.append(_err("IR_UNKNOWN_ITEM", f"Instrument {spec.function}: '{spec.on}' is neither an equipment tag nor a planned connection"))
            continue
        if rule["attach"] == "equipment" and target_eq is None:
            errors.append(_err("IR_BAD_ATTACHMENT", f"Instrument {spec.function} on {spec.on}: rule {rule['id']} puts it on the equipment "
                                                    f"itself — use the equipment tag, not a connection"))
            continue
        if rule["attach"] != "equipment" and target_eq is not None:
            side = "into" if rule["attach"] == "inlet_line" else "out of"
            errors.append(_err("IR_BAD_ATTACHMENT", f"Instrument {spec.function} on {spec.on}: rule {rule['id']} puts it on the connection "
                                                    f"{side} {spec.on}, not on the equipment"))
            continue
        owner_tag = target_eq.tag if target_eq else (line.to.item if rule["attach"] == "inlet_line" else line.from_.item)  # type: ignore[union-attr]
        owner = eq_by_tag.get(owner_tag)
        if owner is None:
            errors.append(_err("IR_BAD_ATTACHMENT", f"Instrument {spec.function} on {spec.on}: rule {spec.rule} applies to equipment, but the connection's owner {owner_tag} is not equipment"))
            continue
        if not rule_applies(rule, owner):
            errors.append(_err("IR_RULE_NOT_APPLICABLE", f"Instrument {spec.function} on {spec.on}: rule {rule['id']} does not apply to "
                                                         f"{owner.tag} ({owner.type}) — remove it or use the rule for that equipment"))
            continue
        loop_no = tagger.loop_number(f"{rule['id']}:{owner.stage}#{owner.train}", owner.area)
        rule_spec = next((i for i in rule["instruments"] if i["function"] == spec.function), None)
        if rule_spec is None:
            errors.append(_err("IR_RULE_MISMATCH", f"Instrument {spec.function} on {spec.on}: rule {rule['id']} requires "
                                                   f"{', '.join(i['function'] for i in rule['instruments'])}, not {spec.function}"))
            continue
        location = rule_spec.get("location", "field")
        inst_tag = tagger.instrument(spec.function, loop_no)
        if inst_tag in planned_as:
            errors.append(_err("IR_DUPLICATE_ENTRY", f"Instrument {spec.function} ({rule['id']}) for {owner.tag} is planned twice — on "
                                                     f"'{planned_as[inst_tag]}' and on '{spec.on}'; the rule needs it once, keep one"))
            continue
        planned_as[inst_tag] = spec.on
        inst = Instrument(
            tag=inst_tag, function=spec.function, type=symbols[location], location=location,
            attached_to=InstrumentAttachment(kind="equipment", ref=owner.tag) if target_eq else InstrumentAttachment(kind="line", ref=line.tag),  # type: ignore[union-attr]
            alarms=list(spec.alarms), provenance=Provenance(agent=AGENT, rule=rule["id"], rationale=rule["rationale"]),
        )
        instruments.append(inst)
        by_place[(spec.function, spec.on.replace(" ", ""))] = inst
        # whether an instrument has an in-line primary element is defined by the rule, not by the plan
        if rule_spec.get("inline") and line is not None:
            inline[line.tag].append(InlineComponent(tag=f"FE-{loop_no}", type="flow_element", position=0.6,
                                                    provenance=Provenance(agent=AGENT, rule=rule["id"], rationale=rule["rationale"])))

    # ---- control loops: the plan says which loop exists; the rule says how it is built ---------------------
    loops: list[ControlLoop] = []
    loop_entries: set[tuple[str, str]] = set()
    for lp in plan.loops:
        rule = inst_rules.get(lp.rule)
        eq = eq_by_tag.get(lp.equipment.strip())
        if rule is None:
            errors.append(_err("IR_UNKNOWN_RULE", f"Loop for {lp.equipment}: '{lp.rule}' is not an instrumentation rule"))
            continue
        loop_cfg = rule.get("loop")
        if not loop_cfg:
            errors.append(_err("IR_RULE_MISMATCH", f"Loop for {lp.equipment}: rule {rule['id']} defines no control loop — remove the loop entry"))
            continue
        if eq is None:
            errors.append(_err("IR_UNKNOWN_ITEM", f"Loop {rule['id']}: '{lp.equipment}' is not an equipment tag"))
            continue
        if not rule_applies(rule, eq):
            errors.append(_err("IR_RULE_NOT_APPLICABLE", f"Loop {rule['id']} does not apply to {eq.tag} ({eq.type}) — remove the loop entry"))
            continue
        loop_no = tagger.loop_number(f"{rule['id']}:{eq.stage}#{eq.train}", eq.area)
        ctrl_tag = f"{loop_cfg['controller']}-{loop_no}"
        if (rule["id"], eq.tag) in loop_entries:
            errors.append(_err("IR_DUPLICATE_ENTRY", f"Loop {rule['id']} for {eq.tag} is listed twice — keep one"))
            continue
        loop_entries.add((rule["id"], eq.tag))
        measured_fn = rule["instruments"][0]["function"]
        measured = next((i for i in instruments if i.tag == tagger.instrument(measured_fn, loop_no)), None)
        if measured is None:
            errors.append(_err("IR_LOOP_MEASUREMENT_MISSING", f"Loop {loop_cfg['controller']} ({rule['id']}) for {eq.tag}: plan the measuring "
                                                              f"instrument {measured_fn} for {eq.tag} under rule {rule['id']} first"))
            continue
        prov = Provenance(agent=AGENT, rule=rule["id"], rationale=rule["rationale"])
        kind = loop_cfg["final_element"]
        final: str | None = None
        final_kind = "valve"
        if kind == "utility_valve":
            final = f"{loop_cfg['controller'][0]}CV-{loop_no}"
            instruments.append(Instrument(tag=final, function=f"{loop_cfg['controller'][0]}CV", type=symbols["utility_valve"],
                                          attached_to=InstrumentAttachment(kind="equipment", ref=eq.tag, port="utility"),
                                          loop=ctrl_tag, provenance=prov))
        elif kind == "self_vfd":
            if eq.type != "centrifugal_pump":
                errors.append(_err("IR_NO_FINAL_ELEMENT", f"Loop {ctrl_tag} ({rule['id']}): {eq.tag} is not a pump with a drive"))
                continue
            final, final_kind = eq.tag, "vfd"
        elif kind in LINE_VALVE_FINAL:
            ctl = attachment_line(LINE_VALVE_FINAL[kind], eq, lines)
            if ctl is None:
                errors.append(_err("IR_NO_FINAL_ELEMENT", f"Loop {ctrl_tag} ({rule['id']}): {eq.tag} has no "
                                                          f"{LINE_VALVE_FINAL[kind].replace('_', ' ')} for the control valve"))
                continue
            final = f"{loop_cfg['controller'][0]}CV-{loop_no}"
            inline[ctl.tag].append(InlineComponent(tag=final, type="control_valve", position=0.45, provenance=prov))
        elif kind == "outlet_diversion_valve":
            out = attachment_line("outlet_line", eq, lines)
            if out is not None:
                final = f"FDV-{loop_no}"
                inline[out.tag].append(InlineComponent(tag=final, type=symbols["diversion_valve"], position=0.35, provenance=prov))
            else:
                errors.append(_err("IR_NO_FINAL_ELEMENT", f"Loop {ctrl_tag} ({rule['id']}): {eq.tag} has no outgoing connection for the diversion valve"))
                continue
        else:
            up = kind.startswith("upstream")
            final, final_kind = _walk_to_pump(eq.tag, lines, kinds, upstream=up), "vfd"
            if final is None:
                errors.append(_err("IR_NO_FINAL_ELEMENT", f"Loop {ctrl_tag} ({rule['id']}): its final element is the VFD of the nearest pump "
                                                          f"{'upstream of' if up else 'downstream of'} {eq.tag}, but no pump is reachable along a single "
                                                          f"process path (a header or branch is in between) — check the connections around {eq.tag}"))
                continue
        if not any(i.tag == ctrl_tag for i in instruments):  # an interlock switch is its own controller (TSL)
            instruments.append(Instrument(tag=ctrl_tag, function=loop_cfg["controller"], type=symbols["dcs"], location="dcs",
                                          attached_to=measured.attached_to, provenance=prov))
        for i in instruments:
            if i.tag.endswith(f"-{loop_no}") and i.provenance.rule == rule["id"]:
                i.loop = ctrl_tag
        loops.append(ControlLoop(tag=ctrl_tag, measured_by=measured.tag, final_element=final,
                                 final_element_kind=final_kind, setpoint=loop_cfg.get("setpoint", ""), provenance=prov))  # type: ignore[arg-type]

    # ---- the reviewer's own instruments (their authority, not the planner's) --------------------------
    line_area = {ln.tag: area_of.get(ln.from_.item, "1") for ln in lines}
    for add in ctx.intent.added_instruments:
        area = area_of.get(add.attached_ref) or line_area.get(add.attached_ref)
        if area is None:
            continue
        loop_no = tagger.loop_number(f"added:{add.function}:{add.attached_ref}", area)
        location = "dcs" if add.function.endswith(("IC", "C")) and len(add.function) >= 3 else "field"
        instruments.append(Instrument(tag=tagger.instrument(add.function, loop_no), function=add.function,
                                      type=symbols[location], location=location,  # type: ignore[arg-type]
                                      attached_to=InstrumentAttachment(kind=add.attached_kind, ref=add.attached_ref),
                                      provenance=Provenance(agent="reviewer", rule="REVIEWER", rationale=add.rationale)))

    for ln in lines:
        ln.inline = sorted(ln.inline, key=lambda c: c.position)
    return Compiled(
        topology=TopologyProposal(agent=AGENT, piping_nodes=nodes, lines=lines, used_llm=True),
        instrumentation=InstrumentationProposal(agent=AGENT, instruments=instruments, loops=loops, inline=dict(inline), used_llm=True),
        errors=errors,
        connection_of_line=connection_of_line,
        node_of_tag={t: nid for nid, t in node_tag.items()},
    )


def _terminal_stage(kind: str, near: str | None, eq_by_tag: dict[str, Equipment], process: ProcessDefinition) -> str | None:
    """The template stage a planned battery limit stands for: same kind, in the module and chain of
    the equipment it connects to (a module drawing has several terminals of each kind)."""
    stage_by_id = {st.id: st for st in process.stages}
    here = stage_by_id.get(eq_by_tag[near].stage) if near in eq_by_tag else None
    candidates = [st for st in process.stages if st.kind == kind]
    if here is not None:
        same_module = [st for st in candidates if st.module == here.module] or candidates
        candidates = [st for st in same_module if st.branch == here.branch] or same_module
    return candidates[0].id if candidates else None


def _nearest_equipment(start: str, neighbours: dict[str, list[str]], eq_by_tag: dict[str, Equipment]) -> str | None:
    """Closest equipment to a planned node, looking through headers and tees (breadth first)."""
    seen, frontier = {start}, [start]
    while frontier:
        nxt = []
        for x in frontier:
            for y in neighbours[x]:
                if y in eq_by_tag:
                    return y
                if y not in seen:
                    seen.add(y)
                    nxt.append(y)
        frontier = nxt
    return None
