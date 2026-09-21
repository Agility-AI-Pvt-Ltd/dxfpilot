"""Topology Agent — what connects to what, in which order; headers, branches, bypasses, line sizes
and the piping valves required by line rules. Fully deterministic graph construction."""

from __future__ import annotations

from langsmith import traceable

from ..engine import line_rules
from ..engine.flows import LineFlows
from ..engine.merge import consumers
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
from .llm import mark_source

AGENT = "topology_agent"


@traceable(name="topology_agent", run_type="chain", process_inputs=lambda d: d["ctx"].trace_summary())
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
    module_of = {st.id: st.module for st in process.stages}

    for st in process.stages:
        count = process.groups.get(st.group, 1)
        if st.kind == "equipment":
            items = sorted((e for e in equipment if e.stage == st.id), key=lambda e: e.train or 0)
            members[st.id] = [e.tag for e in items]
            for e in items:
                kinds[e.tag] = e.type
        else:
            users = consumers(st.utility_users, st.module, equipment, module_of) if st.utility_users else []
            train_labels = process.group_labels.get(st.group, [])
            tags = []
            for train in range(1, count + 1):
                tag = tagger.piping_node(f"{st.id}#{train}", st.kind, st.area)
                label = tpl_stages[st.id].get("label", st.name)
                if train <= len(users):  # a utility outlet names the consumer it serves
                    label = f"{label} {users[train - 1].tag} {users[train - 1].name.split(' — ')[0].split(' (')[0]}".upper()
                elif train <= len(train_labels):
                    label = f"{label} — {train_labels[train - 1]}".upper()
                other = next((o for o in process.stages if o.id == st.connects_to), None)
                if other is not None:  # the connected module is on this drawing: say where
                    label = f"{label} (AREA {other.area})"
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
    # (from, to, service, from port override, to port override)
    edges: list[tuple[str, str, str, str | None, str | None]] = []
    port_counter: dict[str, int] = {}
    stage_by_id = {st.id: st for st in process.stages}

    def next_port(tag: str, prefix: str) -> str:
        port_counter[f"{tag}:{prefix}"] = port_counter.get(f"{tag}:{prefix}", 0) + 1
        return f"{prefix}{port_counter[f'{tag}:{prefix}']}"

    def connect(a, b, ups: list[str], downs: list[str], service: str, out_port: str | None = None, in_port: str | None = None) -> None:
        """Stage a → stage b: train by train when they match, otherwise through one header."""
        if not ups or not downs:
            warnings.append(f"No items for stage transition {a.id} → {b.id}")
            return
        if len(ups) == len(downs) and (a.group == b.group or a.branch or b.branch or len(ups) == 1):
            # train by train; a single unit feeding a single unit needs no header either
            edges.extend((u, d, service, out_port, in_port) for u, d in zip(ups, downs))
            return
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
        edges.extend((u, hdr, service, out_port, None) for u in ups)
        edges.extend((hdr, d, service, None, in_port) for d in downs)

    # every module (and a classic template) is a main chain plus optional side branches
    chains: dict[tuple[str | None, str | None], list] = {}
    for st in process.stages:
        chains.setdefault((st.module, st.branch), []).append(st)
    for (_module, branch), chain in chains.items():
        service = chain[0].outlet_service or "RM"
        if branch and chain[0].source in members:  # the branch leaves a stage through its second outlet
            src = stage_by_id[chain[0].source]
            carried = chain[0].source_service or src.outlet_service or service  # e.g. cream from a separator
            connect(src, chain[0], members[src.id], members[chain[0].id], carried, out_port="aux_out")
        for a, b in zip(chain, chain[1:]):
            if a.outlet_service:
                service = a.outlet_service
            connect(a, b, members.get(a.id, []), members.get(b.id, []), service)
    for (_module, branch), chain in chains.items():
        last = chain[-1]
        if not branch or not last.feeds or last.feeds not in members:
            continue
        target = stage_by_id[last.feeds]
        downs = [t for i, t in enumerate(members[target.id], 1) if not last.feeds_trains or i in last.feeds_trains]
        fed = {e[1] for e in edges}
        # a unit that already has its main inlet takes the side feed on its auxiliary inlet
        in_port = "aux_in" if any(d in fed for d in downs) else None
        connect(last, target, members.get(last.id, []), downs, last.outlet_service or chain[0].outlet_service or "RM", in_port=in_port)

    # ---- bypasses (reviewer intent) --------------------------------------------------------
    bypass_edges: list[tuple[str, str, str]] = []
    for target in ctx.intent.bypasses:
        ins = [e for e in edges if e[1] == target and e[4] is None]
        outs = [e for e in edges if e[0] == target and e[3] is None]
        if len(ins) != 1 or len(outs) != 1:
            warnings.append(f"Bypass around {target} skipped: it needs exactly one inlet and one outlet line")
            continue
        (a_, _, s_in, ap, _), (_, b_, s_out, _, bp) = ins[0], outs[0]
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
        edges += [(a_, j1, s_in, ap, None), (j1, target, s_in, None, None), (target, j2, s_out, None, None), (j2, b_, s_out, None, bp)]
        bypass_edges.append((j1, j2, s_in, None, None))

    # ---- flows (shared with the IR compiler) ----------------------------------------------------
    train_of = {e.tag: e.train for e in equipment} | {n.tag: n.train for n in nodes}
    links = [(a, b) for st in process.stages if st.connects_to in members
             for a in members[st.id] for b in members[st.connects_to]]
    links += [(b, a) for a, b in list(links) if kinds.get(a) == "terminal_in"]  # declared on the inlet side
    links = [(a, b) for a, b in links if kinds.get(a) == "terminal_out"]
    flows = LineFlows([(e[0], e[1]) for e in edges], kinds, group_of, train_of, equipment, ctx.template, links)
    all_edges = edges + bypass_edges

    # ---- lines + line-rule valves ---------------------------------------------------------
    spec = rules["spec"]["default"]
    mod_of_area = line_rules.module_of_area(process)

    lines: list[Line] = []
    for u, d, svc, out_port, in_port in all_edges:
        is_bypass = (u, d, svc, out_port, in_port) in bypass_edges
        key = f"{u}>{d}"
        area = area_of.get(u) or area_of.get(d) or "1"
        tag = tagger.line(key, svc, area)
        f = flows.line_flow(u, d)
        from_port = out_port or ("outlet" if kinds.get(u) not in ("header", "tee") else ("branch" if is_bypass else next_port(u, "out")))
        to_port = in_port or ("inlet" if kinds.get(d) not in ("header", "tee") else ("branch" if is_bypass else next_port(d, "in")))
        kind = "bypass" if is_bypass else "process"
        inline: list[InlineComponent] = []
        for vr in rules["valves"]:
            if not line_rules.in_scope(vr, area, mod_of_area) or not line_rules.applies(vr["applies_to"], kinds.get(u), kinds.get(d), kind, svc):
                continue
            # identity follows what the valve serves, so re-routing a line keeps its valves
            vtag = tagger.valve(f"{vr['id']}:{line_rules.owner(vr['applies_to'], u, d)}", vr["class"], area)
            inline.append(
                InlineComponent(
                    tag=vtag,
                    type=vr["type"],
                    position=vr["position"],
                    provenance=Provenance(agent=AGENT, rule=vr["id"], rationale=vr["rationale"]),
                )
            )
        value, unit = line_rules.flow_quantity(rules, svc, f) if f else (0, "")
        lines.append(
            Line(
                tag=tag,
                **{"from": PortRef(item=u, port=from_port)},
                to=PortRef(item=d, port=to_port),
                service=svc,
                stream=svc,
                kind=kind,
                design_flow=Quantity(value=value, unit=unit) if f else None,
                size_dn=line_rules.size(rules, svc, f) if f else None,
                spec=rules["spec"].get("by_service", {}).get(svc, spec),
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

    mark_source("rules", rules="process template + line_rules.yaml")
    return TopologyProposal(piping_nodes=nodes, lines=lines, warnings=warnings)
