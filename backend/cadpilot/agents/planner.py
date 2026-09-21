"""LLM Planner (experimental engine) — proposes the P&ID plan (IR): connections, headers, valves,
instruments and control loops for equipment the equipment agent already chose.

It works *for* the validator, never around it: the rule catalogues are given as read-only
reference, the IR schema has no field that could alter a rule or waive a check, and on a failed
validation the planner only receives the errors and returns a corrected plan.
"""

from __future__ import annotations

import json
import re

from langsmith import traceable

from ..model.engineering import Equipment, ProcessDefinition
from ..model.ir import PIDPlan, PlanPatch, apply_patch
from ..model.proposals import ValidationIssue
from .context import AgentContext
from .instrumentation import rule_applies
from .llm import get_llm, mark_source

AGENT = "llm_planner"
MAX_ATTEMPTS = 10  # hard cap: 1 plan + up to 9 corrections (only failing sections are re-sent)
NO_PROGRESS_LIMIT = 3  # stop early when this many corrections in a row fail to beat the best plan
PLANNER_TIMEOUT_S = 150  # a full plan is a large structured output

SYSTEM = """You are the P&ID planner of CadPilot, an engineering drafting system for dairy plants.
Given the equipment already selected, propose the complete P&ID plan: battery-limit terminals,
headers, every process connection, the valves required by the line rules and the instruments and
control loops required by the instrumentation rules.

Hard constraints:
- Use equipment tags exactly as listed. Do not invent equipment.
- Every valve, instrument and loop must name the rule that requires it. You cannot change, relax or
  skip a rule; a deterministic validator checks your plan against them and is the authority.
- Tags, line numbers, pipe sizes and flows are computed by the system — do not include them.
- Connections are directed in the flow direction. Refer to a connection as "SOURCE>TARGET".
"""

GUIDANCE = """Conventions:
- One terminal_in per reception train (label from the template) feeding that train's first unit,
  and one terminal_out per processing train after its last unit.
- Units of the same train group connect 1:1 train by train (train 1 → train 1, ...).
- Where one train group feeds another group with a different number of units, route through ONE
  header: every upstream unit → header, header → every downstream unit. Never connect such units
  directly. Each such boundary gets its OWN header (e.g. one into the silos, another out of them).
- Engineering modules are drawn separately: connect stages only within the same module and chain,
  never across modules (they meet at their battery-limit terminals). A side chain runs on its own and
  joins where it says (feeds X: its last units connect into X; starts from the second outlet of Y).
- Add nothing that is not required: no bypasses, tees, extra headers or extra valves unless a rule
  or a reviewer requirement below asks for it.
- Line-rule meaning: pump_suction = the connection INTO a pump; pump_discharge = the connection OUT
  of a pump; header_branch = every connection to or from a header; bypass = the bypass connection.
- Follow the CONNECTION PLAN and the REQUIRED ITEMS checklist below: they are the rules applied to
  this equipment. Every checklist item exactly once, and nothing that is not on it.
- Never: an on_off_valve LR-MANIFOLD-ROUTE on a connection that does not touch a header; a loop
  entry for a rule without a LOOP; an outlet_line/inlet_line instrument "on" the equipment tag
  (it goes on the connection 'TAG>NEXT' / 'PREV>TAG'); a connection from a node to itself; a
  connection or valve that refers to a node you did not declare; two connections on the same inlet
  or outlet of a unit (that is what a header is for); a node for a piece of equipment (equipment is
  referred to only by its tag, e.g. S-901 — nodes are only terminals, headers and tees); a
  terminal_out as the source of a connection or a terminal_in as its target; a valve or instrument
  "on" a connection that is not written exactly as in your connections list.
- Instrumentation-rule meaning: attach 'equipment' → instrument "on" the equipment tag; 'inlet_line' /
  'outlet_line' → instrument "on" the connection into / out of that equipment. List each of the rule's
  instruments exactly once per equipment the rule applies to.
- Control loops: for a rule that has a loop, add ONE loop entry {rule, equipment} for every equipment
  it applies to (as well as the rule's instruments). The system builds the controller, the final
  control element and the setpoint from the rule. Never list a controller (TIC, LIC, FIC, ...) as an
  instrument. A pump-VFD loop (upstream_pump_vfd / downstream_pump_vfd) needs a single process path
  from the equipment to that pump, with no header in between."""


def _catalogues(ctx: AgentContext, equipment: list[Equipment] | None = None, modules: set[str | None] | None = None) -> str:
    """The rule catalogues, limited to what can apply here (shared rules + the modules' own)."""
    def relevant(rule: dict) -> bool:
        return modules is None or rule.get("module") in modules | {None}

    lr = "\n".join(
        f"- {v['id']}: applies_to={v['applies_to']} type={v['type']} — {v['rationale']}"
        for v in ctx.standards.line_rules["valves"] if relevant(v)
    )
    ir = []
    for r in ctx.standards.instrumentation["rules"]:
        if equipment is not None and not any(rule_applies(r, e) for e in equipment):
            continue  # a rule for equipment that is not in this scope
        insts = ", ".join(
            f"{i['function']}{' alarms=' + '/'.join(i['alarms']) if i.get('alarms') else ''}{' inline_element' if i.get('inline') else ''}"
            for i in r["instruments"]
        )
        loop = r.get("loop")
        loop_txt = f"; loop controller={loop['controller']} final={loop['final_element']} setpoint={loop.get('setpoint', '')}" if loop else ""
        on = [e.tag for e in equipment or [] if rule_applies(r, e)]
        where = f"\n    → required for EVERY one of: {', '.join(on)}" if on else ""
        kind = "LOOP" if loop else "no loop"
        ir.append(f"- {r['id']} [{kind}]: applies_to={r['applies_to']} attach={r['attach']} instruments=[{insts}]{loop_txt}{where}")
    return f"LINE RULES (read-only):\n{lr}\n\nINSTRUMENTATION RULES (read-only):\n" + "\n".join(ir)


def _listify(v) -> list:
    return v if isinstance(v, list) else ([v] if v else [])


def _requirements(ctx: AgentContext, process: ProcessDefinition, equipment: list[Equipment],
                  all_equipment: list[Equipment] | None = None, prefix: str = "") -> str:
    """A deterministic checklist from the same rule files the validator uses: how the stages connect
    and, per equipment, every valve, instrument and loop it needs and where it goes."""
    from ..engine import line_rules as lr
    from ..engine.merge import consumers

    std = ctx.standards
    tpl = {t["id"]: t for t in ctx.template["stages"]}
    by_stage: dict[str, list[Equipment]] = {}
    for e in sorted(equipment, key=lambda e: (e.stage, e.train or 0)):
        by_stage.setdefault(e.stage, []).append(e)
    count = {st.id: (len(by_stage.get(st.id, [])) if st.kind == "equipment" else process.groups.get(st.group, 1)) for st in process.stages}

    def who(st) -> str:
        if st.kind == "equipment":
            return ", ".join(e.tag for e in by_stage.get(st.id, [])) or "(none)"
        return f"{count[st.id]} {st.kind} node(s) '{tpl.get(st.id, {}).get('label', st.name)}'"

    route_rule = next((v for v in std.line_rules["valves"] if v["id"] == "LR-MANIFOLD-ROUTE"), None)
    utility_services = set(_listify(route_rule["applies_to"].get("not_service"))) if route_rule and isinstance(route_rule["applies_to"], dict) else set()
    service_of = {}
    for chain_stages in [process.stages]:
        svc = None
        for st in chain_stages:
            service_of[st.id] = st.outlet_service or svc
            svc = st.outlet_service or svc

    def one_to_one(a, b, n_a: int, n_b: int) -> bool:  # same rule as the topology agent
        return n_a == n_b and (a.group == b.group or bool(a.branch or b.branch) or n_a == 1)

    def how(a, b, n_a: int, n_b: int) -> str:
        if one_to_one(a, b, n_a, n_b):
            return ("train by train (1st → 1st, 2nd → 2nd, …)" if n_a > 1 else "directly") + "; no route valve"
        route = ("; every connection to and from this header carries on_off_valve LR-MANIFOLD-ROUTE"
                 if route_rule and service_of.get(a.id) not in utility_services else "; no route valve (utility main)")
        return f"through ONE header of their own: each of the {n_a} → header → each of the {n_b}{route}"

    # the exact neighbour of every unit, where the plan fixes it (for the checklist below)
    nxt: dict[str, str] = {}
    prv: dict[str, str] = {}
    for chain_stages in [[st for st in process.stages if st.branch == br] for br in dict.fromkeys(st.branch for st in process.stages)]:
        for a, b in zip(chain_stages, chain_stages[1:]):
            ua, ub = by_stage.get(a.id, []), by_stage.get(b.id, [])
            if not one_to_one(a, b, count[a.id], count[b.id]):
                for e in ua:
                    nxt[e.tag] = f"'{e.tag}>' + your header id"
                for e in ub:
                    prv[e.tag] = f"your header id + '>{e.tag}'"
                continue
            for i in range(count[a.id]):
                src = ua[i].tag if i < len(ua) else None
                dst = ub[i].tag if i < len(ub) else None
                if src:
                    nxt[src] = f"'{src}>{dst}'" if dst else f"'{src}>' + your {b.kind} node id"
                if dst:
                    prv[dst] = f"'{src}>{dst}'" if src else f"your {a.kind} node id + '>{dst}'"
    # side-chain joins: the last units feeding a stage, and the first units fed from a second outlet
    for st in process.stages:
        if st.feeds in by_stage and st.id in by_stage:
            targets = [e for i, e in enumerate(by_stage[st.feeds], 1) if not st.feeds_trains or i in st.feeds_trains]
            srcs = by_stage[st.id]
            if len(srcs) == len(targets):
                for a_, b_ in zip(srcs, targets):
                    nxt[a_.tag] = f"'{a_.tag}>{b_.tag}'"
            else:
                for a_ in srcs:
                    nxt[a_.tag] = f"'{a_.tag}>' + your header id"
        if st.source in by_stage and st.id in by_stage:
            srcs, dsts = by_stage[st.source], by_stage[st.id]
            for d_ in dsts:
                prv[d_.tag] = (f"'{srcs[dsts.index(d_)].tag}>{d_.tag}'" if len(srcs) == len(dsts) else f"your header id + '>{d_.tag}'")

    out = ["CONNECTION PLAN (the process template applied to this equipment — follow it):"]
    if prefix:
        out.append(f"- Every node id you declare starts with '{prefix}' (e.g. '{prefix}hdr_1').")
    chains: dict[str | None, list] = {}
    for st in process.stages:
        chains.setdefault(st.branch, []).append(st)
    stage_by_id = {st.id: st for st in process.stages}
    for branch, chain in chains.items():
        if branch:
            out.append(f"Side chain '{branch}':")
        first = chain[0]
        if first.source in stage_by_id:
            src = stage_by_id[first.source]
            out.append(f"- {src.name} [{who(src)}] SECOND outlet → {first.name} [{who(first)}]: "
                       f"{how(src, first, count[src.id], count[first.id])}")
        for a, b in zip(chain, chain[1:]):
            out.append(f"- {a.name} [{who(a)}] → {b.name} [{who(b)}]: {how(a, b, count[a.id], count[b.id])}")
        last = chain[-1]
        if branch and last.feeds in stage_by_id:
            tgt = stage_by_id[last.feeds]
            targets = [e for i, e in enumerate(by_stage.get(tgt.id, []), 1) if not last.feeds_trains or i in last.feeds_trains]
            out.append(f"- {last.name} [{who(last)}] → into {', '.join(e.tag for e in targets)} (their second inlet): "
                       f"{how(last, tgt, count[last.id], len(targets))}")
    module_of = {t["id"]: t.get("module") for t in ctx.template["stages"]}
    for st in process.stages:
        if st.utility_users and all_equipment is not None:
            users = consumers(st.utility_users, st.module, all_equipment, module_of)
            if users:
                out.append(f"- {st.name}: one terminal_out per consumer ({len(users)}), labelled "
                           + "; ".join(f"'{tpl.get(st.id, {}).get('label', '')} {u.tag}'" for u in users))

    mods = {st.module for st in process.stages}
    rules = [v for v in std.line_rules["valves"] if v.get("module") in mods | {None}]
    out.append("\nREQUIRED ITEMS PER EQUIPMENT (each exactly once; nothing else):")
    for e in sorted(equipment, key=lambda e: e.tag):
        items = []
        for v in rules:
            at = v["applies_to"]
            into = at == "pump_suction" and e.type == "centrifugal_pump" or isinstance(at, dict) and e.type in _listify(at.get("to_type"))
            outof = at == "pump_discharge" and e.type == "centrifugal_pump" or isinstance(at, dict) and e.type in _listify(at.get("from_type"))
            svc = f" (only if the service is {'/'.join(_listify(at['service']))})" if isinstance(at, dict) and at.get("service") else ""
            if into:
                items.append(f"valve {v['type']} rule {v['id']} on the connection INTO {e.tag} ({prv.get(e.tag, 'PREV>' + e.tag)}){svc}")
            if outof:
                items.append(f"valve {v['type']} rule {v['id']} on the connection OUT OF {e.tag} ({nxt.get(e.tag, e.tag + '>NEXT')}){svc}")
        for r in std.instrumentation["rules"]:
            if not rule_applies(r, e):
                continue
            where = {"equipment": f"on '{e.tag}'",
                     "inlet_line": f"on the connection INTO {e.tag} ({prv.get(e.tag, f'PREV>{e.tag}')})",
                     "outlet_line": f"on the connection OUT OF {e.tag} ({nxt.get(e.tag, f'{e.tag}>NEXT')})"}[r["attach"]]
            for spec in r["instruments"]:
                alarms = f" alarms {spec['alarms']}" if spec.get("alarms") else ""
                items.append(f"instrument {spec['function']}{alarms} {where} rule {r['id']}")
            if r.get("loop"):
                items.append(f"loop {{rule: {r['id']}, equipment: {e.tag}}}")
        out.append(f"- {e.tag} ({e.name}): " + ("; ".join(items) if items else "no rule items"))
    general = [v for v in rules if v["applies_to"] in ("header_branch",) or (isinstance(v["applies_to"], dict) and (
        v["applies_to"].get("header_branch") or set(_listify(v["applies_to"].get("to_type")) + _listify(v["applies_to"].get("from_type")))
        & {"terminal_in", "terminal_out", "header"}))]
    if general:
        out.append("Valves on connections by what they touch:")
        for v in general:
            out.append(f"- {v['type']} rule {v['id']} on every one of the {lr.describe(v['applies_to'])} — and on no other connection")
    return "\n".join(out)


def _context(ctx: AgentContext, process: ProcessDefinition, equipment: list[Equipment],
             all_equipment: list[Equipment] | None = None, prefix: str = "") -> str:
    def extra(s) -> str:
        bits = []
        if s.module:
            bits.append(f"module {s.module}")
        if s.branch:
            bits.append(f"side chain '{s.branch}'")
        if s.source:
            bits.append(f"starts from the second outlet of {s.source}")
        if s.feeds:
            bits.append(f"feeds {s.feeds}" + (f" trains {s.feeds_trains}" if s.feeds_trains else ""))
        return f" [{'; '.join(bits)}]" if bits else ""

    stages = "\n".join(
        f"- {s.id} ({s.kind}, group {s.group}, {process.groups.get(s.group, 1)} unit(s)){' label=' + repr(next((t.get('label') for t in ctx.template['stages'] if t['id'] == s.id), '')) if s.kind != 'equipment' else ''}{extra(s)}"
        for s in process.stages
    )
    eq = "\n".join(
        f"- {e.tag} | {e.type} | stage {e.stage} | train {e.train} | {e.capacity or 'no capacity'}" for e in equipment
    )
    intent = ctx.intent
    reviewer = []
    if intent.bypasses:
        reviewer.append(
            f"Bypass required around: {', '.join(intent.bypasses)}. For each: declare two tee nodes, route "
            "upstream → tee_in → unit → tee_out → downstream (process connections), and add one 'bypass' "
            "connection tee_in → tee_out carrying the LR-BYPASS valve."
        )
    if intent.suppressed:
        reviewer.append(f"Items the reviewer removed (the system drops them): {', '.join(intent.suppressed)}")
    if intent.added_instruments:
        reviewer.append("Reviewer-added instruments are added by the system; do not add them yourself.")
    mods = {st.module for st in process.stages}
    return (
        f"Process stages in flow order:\n{stages}\n\nEquipment (use these tags):\n{eq}\n\n"
        + (("Reviewer requirements:\n" + "\n".join(reviewer) + "\n\n") if reviewer else "")
        + f"{_catalogues(ctx, equipment, mods)}\n\n{GUIDANCE}\n\n{_requirements(ctx, process, equipment, all_equipment, prefix)}"
    )


FIX_ORDER = {"structural": 1, "topology": 1}


def fix_order(issue: ValidationIssue) -> int:
    """Plan errors first, then connections, then rule items (which often follow from the connections)."""
    return 0 if issue.code.startswith("IR_") else FIX_ORDER.get(issue.layer, 2)


def _neighbours(model, ctx: AgentContext, tag: str, downstream: bool, node_of_tag: dict[str, str]) -> str | None:
    """The units of the next (or previous) stage in flow order, and how the template connects to them."""
    eq = next((e for e in model.equipment if e.tag == tag), None)
    if eq is None:
        return None
    here_stage = next((st for st in ctx.template["stages"] if st["id"] == eq.stage), {})
    # flow order within the same module and chain (modules meet only at battery limits)
    order = [st for st in ctx.template["stages"]
             if st.get("module") == here_stage.get("module") and st.get("branch") == here_stage.get("branch")]
    idx = next((k for k, st in enumerate(order) if st["id"] == eq.stage), None)
    if idx is None:
        return None
    step = 1 if downstream else -1
    k = idx + step
    while 0 <= k < len(order):
        st = order[k]
        members = [e for e in model.equipment if e.stage == st["id"]]
        nodes = [n for n in model.piping_nodes if n.stage == st["id"]]
        if members or nodes:
            here = [e for e in model.equipment if e.stage == eq.stage]
            names = [e.tag for e in sorted(members, key=lambda e: e.train or 0)] or [node_of_tag.get(n.tag, n.kind) for n in nodes]
            if st.get("group") == order[idx].get("group") or len(names) == len(here):
                same = next((e.tag for e in members if e.train == eq.train), None)
                target = same or (names[(eq.train or 1) - 1] if len(names) >= (eq.train or 1) else names[0])
                return f"{tag}>{target}" if downstream else f"{target}>{tag}"
            peers = sorted(e.tag for e in here)
            other = [e.tag for e in members]
            direct = [f"{ln.from_.item}>{ln.to.item}" for ln in model.lines
                      if (ln.from_.item in peers and ln.to.item in other) or (ln.from_.item in other and ln.to.item in peers)]
            # the header on the other side of this unit must not be reused: that would make a loop
            other_side = sorted({node_of_tag.get(x, x) for ln in model.lines
                                 for x in [ln.from_.item if downstream else ln.to.item]
                                 if (ln.to.item if downstream else ln.from_.item) == tag and x in node_of_tag})
            ups, downs = (peers, names) if downstream else (names, peers)
            recipe = (f"a NEW header between {', '.join(ups)} and {', '.join(downs)}: each of {', '.join(ups)}>new_header, "
                      f"new_header>each of {', '.join(downs)}")
            if other_side:
                recipe += f"; do not reuse {', '.join(other_side)} ({'it feeds' if downstream else 'it is fed by'} {tag} — that makes a loop)"
            if direct:
                recipe += f"; remove the direct connections {', '.join(direct)}"
            return recipe
        k += step
    return None


def expected_location(issue: ValidationIssue, model, ctx: AgentContext, connection_of_line: dict[str, str],
                      node_of_tag: dict[str, str] | None = None) -> str:
    """Where the validator expects the missing (or the one) item, expressed as the planner's own ids."""
    nodes = node_of_tag or {}
    if issue.code in ("NO_OUTLET", "NO_INLET") and issue.refs:
        where = _neighbours(model, ctx, issue.refs[0], issue.code == "NO_OUTLET", nodes)
        return f"expected (flow order of the stages): connection {where}" if where else ""
    if issue.code == "ORPHAN" and issue.refs and issue.refs[0] in nodes:
        return f"remove it (remove_nodes: ['{nodes[issue.refs[0]]}']) unless the connection plan needs it"
    if issue.code == "CYCLE" and issue.refs:
        loop = [nodes.get(r, r) for r in issue.refs]
        return (f"the loop is {' > '.join(loop + loop[:1])} — every connection must follow the flow order of the stages; "
                f"a header either collects from one stage and feeds the next, never both ways around the same unit")
    if issue.code == "PORT_REUSED" and issue.refs:
        item = issue.refs[0]
        outlet = ".outlet" in issue.message
        conns = [connection_of_line.get(ln.tag, ln.tag) for ln in model.lines
                 if ln.kind != "bypass" and (ln.from_.item if outlet else ln.to.item) == item]
        return (f"its connections now: {', '.join(conns)} — an equipment or terminal {'outlet' if outlet else 'inlet'} takes exactly one; "
                f"keep the one that follows the flow order and route any split or merge through a header") if conns else ""
    if issue.code not in ("MISSING_INSTRUMENT", "MISSING_LOOP", "MISSING_VALVE") or not issue.rule:
        return ""
    if issue.code == "MISSING_VALVE":
        rule = next((v for v in ctx.standards.line_rules["valves"] if v["id"] == issue.rule), None)
        where = [connection_of_line[r] for r in issue.refs if r in connection_of_line]
        return f"expected: {rule['type']} on {where[0]} (rule {issue.rule})" if rule and where else ""
    rule = next((r for r in ctx.standards.instrumentation["rules"] if r["id"] == issue.rule), None)
    eq = issue.refs[0] if issue.refs else None
    if rule is None or eq is None:
        return ""

    def on_for(attach: str) -> str | None:
        if attach == "equipment":
            return eq
        lines = [ln for ln in model.lines if ln.kind != "bypass" and (ln.to.item if attach == "inlet_line" else ln.from_.item) == eq]
        return connection_of_line.get(lines[0].tag) if lines else f"(no connection {'into' if attach == 'inlet_line' else 'out of'} {eq} yet)"

    on = on_for(rule["attach"])
    if issue.code == "MISSING_INSTRUMENT":
        fn = next((w for w in issue.message.split() if w.isupper() and any(w == i["function"] for i in rule["instruments"])), rule["instruments"][0]["function"])
        return f"expected: instrument {fn} on {on} (rule {issue.rule})"
    return (f"expected: loop {{rule: {issue.rule}, equipment: {eq}}}, with instrument {rule['instruments'][0]['function']} "
            f"on {on} (rule {issue.rule})")


def signature(issue: ValidationIssue) -> str:
    """Stable identity of an error across attempts (tags of planned items are stable too)."""
    return f"{issue.code}|{issue.rule or ''}|{','.join(sorted(issue.refs)) or issue.message}"


def format_feedback(
    issues: list[ValidationIssue],
    connection_of_line: dict[str, str],
    hints: dict[int, str] | None = None,
    node_of_tag: dict[str, str] | None = None,
    seen: dict[str, list[int]] | None = None,
    current: tuple[int, ...] = (),
) -> str:
    """Validator issues → numbered list the planner can act on, in the planner's own ids.

    Line tags become its connections and piping-node tags its node ids (it never sees our tags).
    Errors it was already told about are marked, so it knows its earlier fix did not work."""
    nodes = node_of_tag or {}

    def name(ref: str) -> str:
        if ref in connection_of_line:
            return f"{ref} (= connection {connection_of_line[ref]})"
        return f"node '{nodes[ref]}'" if ref in nodes else ref

    def rename(text: str) -> str:
        return re.sub(r"\b[A-Z]{2,4}-\d{3,4}\b", lambda m: f"node '{nodes[m.group(0)]}'" if m.group(0) in nodes else m.group(0), text)

    lines = []
    for n, i in enumerate(issues, 1):
        where = [name(r) for r in i.refs if r not in nodes or nodes[r] not in rename(i.message)]
        rule = f" [rule {i.rule}]" if i.rule else ""
        hint = (hints or {}).get(n - 1, "")  # hints are keyed by the error's position
        before = [n for n in (seen or {}).get(signature(i), []) if n not in (current or ())]
        again = f" (STILL UNFIXED — also reported after attempt{'s' if len(before) > 1 else ''} {', '.join(map(str, before))})" if before else ""
        lines.append(f"{n}. [{i.code}]{rule}{again} {rename(i.message)}" + (f" — at {', '.join(where)}" if where else "") + (f" — {hint}" if hint else ""))
    return "\n".join(lines)


# ---- sections: every engineering module is planned and corrected on its own -------------------------------
# A whole-plant plan is one huge structured answer (and one huge correction) — the model loses track and
# the call can time out. Planned per module, each answer is small, runs in parallel, and a correction only
# touches the sections that still have errors.


def module_keys(process: ProcessDefinition) -> list[str]:
    return list(dict.fromkeys(st.module or "" for st in process.stages))


def scope(process: ProcessDefinition, equipment: list[Equipment], module: str) -> tuple[ProcessDefinition, list[Equipment]]:
    stages = [st for st in process.stages if (st.module or "") == module]
    ids = {st.id for st in stages}
    groups = {g: n for g, n in process.groups.items() if any(st.group == g for st in stages)}
    return process.model_copy(update={"stages": stages, "groups": groups}), [e for e in equipment if e.stage in ids]


def section_prefix(module: str, process: ProcessDefinition) -> str:
    return f"{module}/" if module and len(module_keys(process)) > 1 else ""


def prefix_nodes(p: PIDPlan, prefix: str) -> PIDPlan:
    """Give every node of a section a section-local id ('cip/hdr_1'), so sections never clash.
    An id that looks like an equipment tag is left alone: declaring equipment as a node is a plan
    error the compiler must report, not something to hide by renaming."""
    if not prefix:
        return p
    rename = {n.id: prefix + n.id for n in p.nodes if not n.id.startswith(prefix) and not _TAG.fullmatch(n.id)}
    if not rename:
        return p

    def ref(x: str) -> str:
        return ">".join(rename.get(part.strip(), part.strip()) for part in x.split(">"))

    q = p.model_copy(deep=True)
    for n in q.nodes:
        n.id = rename.get(n.id, n.id)
    for c in q.connections:
        c.source, c.target = rename.get(c.source, c.source), rename.get(c.target, c.target)
    for v in q.valves:
        v.on = ref(v.on)
    for i in q.instruments:
        i.on = ref(i.on)
    return q


def merge_plans(plans: dict[str, PIDPlan]) -> PIDPlan:
    parts = list(plans.values())
    return PIDPlan(
        nodes=[x for p in parts for x in p.nodes], connections=[x for p in parts for x in p.connections],
        valves=[x for p in parts for x in p.valves], instruments=[x for p in parts for x in p.instruments],
        loops=[x for p in parts for x in p.loops], notes=[x for p in parts for x in p.notes],
    )


EMPTY_PLAN = PIDPlan(nodes=[], connections=[], valves=[], instruments=[], loops=[], notes=[])

_TAG = re.compile(r"\b[A-Z]{1,4}-\d{3,5}\b")


def modules_of_issue(issue: ValidationIssue, module_of_tag: dict[str, str], connection_of_line: dict[str, str],
                     node_of_tag: dict[str, str], sections: list[str]) -> set[str]:
    """Which sections an error belongs to: through the equipment, lines and nodes it names."""
    if len(sections) == 1:
        return set(sections)
    found: set[str] = set()
    tokens = list(issue.refs) + _TAG.findall(issue.message) + re.findall(r"([a-z_]+)/", issue.message)
    for t in tokens:
        if t in sections:
            found.add(t)
        for x in [t] + connection_of_line.get(t, "").split(">") + [node_of_tag.get(t, "")]:
            x = x.strip()
            if x in module_of_tag:
                found.add(module_of_tag[x])
            elif "/" in x and x.split("/")[0] in sections:
                found.add(x.split("/")[0])
    return found or set(sections)  # unattributable: every section sees it


def in_parallel(jobs: dict[str, "callable"]) -> dict[str, object]:
    """Run one LLM job per section at the same time (tracing and call recording follow each thread)."""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    if len(jobs) == 1:
        return {k: f() for k, f in jobs.items()}
    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        futures = {k: pool.submit(contextvars.copy_context().run, f) for k, f in jobs.items()}
        return {k: f.result() for k, f in futures.items()}


@traceable(name="llm_planner", run_type="chain", process_inputs=lambda d: {"attempt": 1, "section": d.get("prefix") or "all"})
def plan(ctx: AgentContext, process: ProcessDefinition, equipment: list[Equipment],
         all_equipment: list[Equipment] | None = None, prefix: str = "") -> PIDPlan | None:
    """The first, complete plan of one section. Later attempts are corrections (`correct`)."""
    llm = get_llm()
    if not llm.enabled:
        mark_source("rules (fallback: no model configured)")
        return None
    prompt = _context(ctx, process, equipment, all_equipment, prefix) + "\n\nReturn the complete plan."
    result = llm.structured(system=SYSTEM, prompt=prompt, schema=PIDPlan, purpose="llm_planner", timeout_s=PLANNER_TIMEOUT_S)
    mark_source("llm" if result else "rules (fallback: model reply unusable)", attempt=1)
    return prefix_nodes(result, prefix) if result else None


CORRECTION = """Fix the plan with a PATCH, not a new plan: the system applies your additions and removals to
the plan below and keeps everything else exactly as it is.
- In `fixes`, give one entry for EVERY error number above, saying what you change for it.
- Then list the additions and removals that carry out those fixes — nothing else.
- To move an item (wrong connection, wrong equipment), remove it and add it at the right place.
- Removing a node or a connection also removes the valves, instruments and loops on it.
- An error marked STILL UNFIXED means your earlier change did not work: read its 'expected:' part and
  use exactly that connection or equipment."""


@traceable(name="llm_planner_correction", run_type="chain",
           process_inputs=lambda d: {"attempt": d["attempt"], "errors_to_fix": len(d.get("errors") or []),
                                     "base_attempt": d.get("base_attempt"), "section": d.get("prefix") or "all"})
def correct(
    ctx: AgentContext,
    process: ProcessDefinition,
    equipment: list[Equipment],
    attempt: int,
    base: PIDPlan,
    base_attempt: int,
    errors: list[ValidationIssue],
    warnings: list[ValidationIssue],
    connection_of_line: dict[str, str],
    node_of_tag: dict[str, str],
    hints: dict[int, str],
    seen: dict[str, list[int]],
    patch_problems: list[str],
    rejected: str | None = None,
    all_equipment: list[Equipment] | None = None,
    prefix: str = "",
) -> tuple[PIDPlan, PlanPatch, list[str], str] | None:
    """One correction: the planner returns a patch against the best plan so far, applied deterministically."""
    llm = get_llm()
    if not llm.enabled:
        return None
    prompt = _context(ctx, process, equipment, all_equipment, prefix)
    prompt += f"\n\nPLAN TO CORRECT (attempt {base_attempt}, the best so far):\n{base.model_dump_json()}\n"
    if rejected:
        prompt += f"\nNOTE: {rejected}\n"
    prompt += (
        f"\nThis plan FAILED VALIDATION. Errors — every one must be fixed:\n"
        f"{format_feedback(errors, connection_of_line, hints, node_of_tag, seen, current=(base_attempt,))}\n"
    )
    prompt += ("Errors are in fix order: plan errors, then connections, then rule items. Valves and instruments on a "
               "connection you change are checked again after the change.\n")
    if patch_problems:
        prompt += "\nYour previous patch could not be applied in full:\n" + "\n".join(f"- {x}" for x in patch_problems) + "\n"
    if warnings:
        prompt += f"\nWarnings (for information; fix if it does not break a rule):\n{format_feedback(warnings, connection_of_line, None, node_of_tag)}\n"
    prompt += f"\n{CORRECTION}"
    patch = llm.structured(system=SYSTEM, prompt=prompt, schema=PlanPatch, purpose="llm_planner_correction", timeout_s=PLANNER_TIMEOUT_S)
    mark_source("llm" if patch else "none (model reply unusable)", attempt=attempt)
    if patch is None:
        return None
    new, problems, summary = apply_patch(base, patch)
    return prefix_nodes(new, prefix), patch, problems, summary


def summarize(p: PIDPlan) -> str:
    return (f"{len(p.nodes)} nodes, {len(p.connections)} connections, {len(p.valves)} valves, "
            f"{len(p.instruments)} instruments, {len(p.loops)} loops")


def plan_json(p: PIDPlan) -> dict:
    return json.loads(p.model_dump_json())
