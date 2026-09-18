"""Topology Agent — what connects to what, in which order; headers, branches, bypasses, line sizes
and the piping valves required by line rules. Fully deterministic graph construction."""

from __future__ import annotations

from ..engine.sizing import flow_m3h, select_dn
from ..model.engineering import (
    Equipment,
    InlineComponent,
    Line,
    PipingNode,
    PortRef,
    ProcessDefinition,
    Provenance,
    Quantity,
)
from ..model.proposals import TopologyProposal
from .context import AgentContext

AGENT = "topology_agent"


def run(ctx: AgentContext, process: ProcessDefinition, equipment: list[Equipment]) -> TopologyProposal:
    tagger = ctx.tagger
    tpl_stages = {s["id"]: s for s in ctx.template["stages"]}
    rules = ctx.standards.line_rules
    warnings: list[str] = []

    nodes: list[PipingNode] = []
    # stage id -> list of node tags (index = train - 1)
    members: dict[str, list[str]] = {}
    kinds: dict[str, str] = {}  # tag -> equipment type / piping node kind
    group_of: dict[str, str] = {}
    area_of: dict[str, str] = {}

    for st in process.stages:
        count = process.groups.get(st.group, 1)
        if st.kind == "equipment":
            items = sorted((e for e in equipment if e.stage == st.id), key=lambda e: e.train or 0)
            members[st.id] = [e.tag for e in items]
            for e in items:
                kinds[e.tag] = e.type
        else:
            tags = []
            for train in range(1, count + 1):
                tag = tagger.piping_node(f"{st.id}#{train}", st.kind, st.area)
                label = tpl_stages[st.id].get("label", st.name)
                nodes.append(
                    PipingNode(
                        tag=tag,
                        kind=st.kind,
                        label=label,
                        area=st.area,
                        stage=st.id,
                        train=train,
                        provenance=Provenance(agent=AGENT, rationale=st.function),
                    )
                )
                kinds[tag] = st.kind
                tags.append(tag)
            members[st.id] = tags
        for t in members[st.id]:
            group_of[t] = st.group
            area_of[t] = st.area

    # ---- connections ----------------------------------------------------------------------
    edges: list[tuple[str, str, str]] = []  # (from, to, service)
    service = "RM"
    port_counter: dict[str, int] = {}

    def next_port(tag: str, prefix: str) -> str:
        port_counter[f"{tag}:{prefix}"] = port_counter.get(f"{tag}:{prefix}", 0) + 1
        return f"{prefix}{port_counter[f'{tag}:{prefix}']}"

    for a, b in zip(process.stages, process.stages[1:]):
        if a.outlet_service:
            service = a.outlet_service
        ups, downs = members.get(a.id, []), members.get(b.id, [])
        if not ups or not downs:
            warnings.append(f"No items for stage transition {a.id} → {b.id}")
            continue
        if a.group == b.group and len(ups) == len(downs):
            edges += [(u, d, service) for u, d in zip(ups, downs)]
        else:
            hdr = tagger.piping_node(f"header:{a.id}>{b.id}", "header", b.area)
            nodes.append(
                PipingNode(
                    tag=hdr,
                    kind="header",
                    label=f"{a.name} → {b.name} manifold",
                    area=b.area,
                    provenance=Provenance(
                        agent=AGENT,
                        rule="TOPO-MANIFOLD",
                        rationale=f"{len(ups)} {a.group} unit(s) feed {len(downs)} {b.group} unit(s): manifold required",
                    ),
                )
            )
            kinds[hdr] = "header"
            area_of[hdr] = b.area
            edges += [(u, hdr, service) for u in ups] + [(hdr, d, service) for d in downs]

    # ---- bypasses (reviewer intent) --------------------------------------------------------
    bypass_edges: list[tuple[str, str, str]] = []
    for target in ctx.intent.bypasses:
        ins = [e for e in edges if e[1] == target]
        outs = [e for e in edges if e[0] == target]
        if len(ins) != 1 or len(outs) != 1:
            warnings.append(f"Bypass around {target} skipped: it needs exactly one inlet and one outlet line")
            continue
        (a_, _, s_in), (_, b_, s_out) = ins[0], outs[0]
        area = area_of.get(target, "1")
        j1 = tagger.piping_node(f"bypass:{target}:in", "tee", area)
        j2 = tagger.piping_node(f"bypass:{target}:out", "tee", area)
        for j, side in ((j1, "upstream"), (j2, "downstream")):
            nodes.append(
                PipingNode(
                    tag=j,
                    kind="tee",
                    label="",
                    area=area,
                    provenance=Provenance(agent=AGENT, rule="LR-BYPASS", rationale=f"Bypass {side} tee around {target}"),
                )
            )
            kinds[j] = "tee"
            area_of[j] = area
            group_of[j] = group_of.get(target, "")
        edges.remove(ins[0])
        edges.remove(outs[0])
        edges += [(a_, j1, s_in), (j1, target, s_in), (target, j2, s_out), (j2, b_, s_out)]
        bypass_edges.append((j1, j2, s_in))

    # ---- flows ----------------------------------------------------------------------------
    # Each train's design flow is set by its flow-driving stage (the pump); groups without one
    # fall back to the largest flow-rated unit in the group.
    cap = {e.tag: flow_m3h(e.capacity) for e in equipment}
    train_of = {e.tag: e.train for e in equipment} | {n.tag: n.train for n in nodes}
    group_flow: dict[str, float] = {}
    for st in process.stages:
        for t in members.get(st.id, []):
            f = cap.get(t)
            if f:
                group_flow[st.group] = max(group_flow.get(st.group, 0), f)
    train_flow: dict[tuple[str, int | None], float] = {}
    for g, gcfg in ctx.template["groups"].items():
        for e in equipment:
            if e.stage == gcfg.get("flow_stage") and cap.get(e.tag):
                train_flow[(g, e.train)] = cap[e.tag]  # type: ignore[assignment]

    def unit_flow(tag: str) -> float | None:
        g = group_of.get(tag, "")
        return train_flow.get((g, train_of.get(tag))) or group_flow.get(g)

    all_edges = edges + bypass_edges

    def flow_from(tag: str, seen: frozenset = frozenset()) -> float | None:
        if tag in seen:
            return None
        if kinds.get(tag) in ("header", "tee"):
            ins = [f for (u, d, _) in edges if d == tag for f in [flow_from(u, seen | {tag})] if f]
            if kinds.get(tag) == "tee":
                return max(ins) if ins else None
            return sum(ins) if ins else None
        if kinds.get(tag) == "terminal_in" or unit_flow(tag) and group_of.get(tag) in group_flow:
            return unit_flow(tag)
        # volume-type units (silos): outflow = total downstream demand
        downs = [d for (u, d, _) in edges if u == tag]
        demand = 0.0
        for d in downs:
            if kinds.get(d) == "header":
                demand += sum(unit_flow(x) or 0 for (u2, x, _) in edges if u2 == d)
            else:
                demand += unit_flow(d) or 0
        return demand or None

    # ---- lines + line-rule valves ---------------------------------------------------------
    target_v = rules["sizing"]["target_velocity_m_s"]
    series = rules["sizing"]["dn_series"]
    spec = rules["spec"]["default"]
    valve_rules = {v["applies_to"]: [] for v in rules["valves"]}
    for v in rules["valves"]:
        valve_rules[v["applies_to"]].append(v)

    lines: list[Line] = []
    for u, d, svc in all_edges:
        is_bypass = (u, d, svc) in bypass_edges
        key = f"{u}>{d}"
        area = area_of.get(u) or area_of.get(d) or "1"
        tag = tagger.line(key, svc, area)
        f = flow_from(u)
        if kinds.get(u) == "header" and unit_flow(d):
            # a manifold branch carries what the train it feeds draws
            f = unit_flow(d)
        from_port = "outlet" if kinds.get(u) not in ("header", "tee") else ("branch" if is_bypass else next_port(u, "out"))
        to_port = "inlet" if kinds.get(d) not in ("header", "tee") else ("branch" if is_bypass else next_port(d, "in"))
        applies: list[str] = []
        if is_bypass:
            applies.append("bypass")
        if kinds.get(d) == "centrifugal_pump":
            applies.append("pump_suction")
        if kinds.get(u) == "centrifugal_pump":
            applies.append("pump_discharge")
        if "header" in (kinds.get(u), kinds.get(d)):
            applies.append("header_branch")
        inline: list[InlineComponent] = []
        for a in applies:
            for vr in valve_rules.get(a, []):
                # identity follows what the valve serves, so re-routing a line keeps its valves
                owner = {"pump_suction": d, "pump_discharge": u}.get(a, key)
                vtag = tagger.valve(f"{vr['id']}:{owner}", vr["class"], area)
                inline.append(
                    InlineComponent(
                        tag=vtag,
                        type=vr["type"],
                        position=vr["position"],
                        provenance=Provenance(agent=AGENT, rule=vr["id"], rationale=vr["rationale"]),
                    )
                )
        lines.append(
            Line(
                tag=tag,
                **{"from": PortRef(item=u, port=from_port)},
                to=PortRef(item=d, port=to_port),
                service=svc,
                stream=svc,
                kind="bypass" if is_bypass else "process",
                design_flow=Quantity(value=round(f, 2), unit="KLPH") if f else None,
                size_dn=select_dn(f, target_v, series) if f else None,
                spec=spec,
                inline=sorted(inline, key=lambda c: c.position),
                provenance=Provenance(
                    agent=AGENT,
                    rule="LR-BYPASS" if is_bypass else "TOPO-SEQUENCE",
                    rationale=("Normally-closed bypass" if is_bypass else f"Process sequence {u} → {d}"),
                ),
            )
        )
        if not f:
            warnings.append(f"{tag}: design flow could not be determined; line not sized")

    return TopologyProposal(piping_nodes=nodes, lines=lines, warnings=warnings)
