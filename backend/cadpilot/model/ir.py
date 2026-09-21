"""P&ID IR — the structured plan the LLM planner proposes.

It is deliberately narrow: the LLM decides *what connects to what* and *which valves, instruments
and loops exist, under which rule*. It cannot set tags, line numbers, sizes, flows or capacities
(the deterministic compiler computes those), and it has no way to touch validation rules or waive
anything — the validator stays the authority.

References:
  equipment   by its existing tag (e.g. "P-101"); equipment itself comes from the equipment agent
  nodes       by an id the plan declares (e.g. "hdr_silo_in", "tanker_1")
  connections as "SOURCE>TARGET" using those tags/ids (e.g. "P-101>E-101")
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class IRNode(BaseModel):
    id: str = Field(description="Id you choose, e.g. 'tanker_1', 'hdr_to_silos', 'to_pmst_2'")
    kind: Literal["terminal_in", "terminal_out", "header", "tee"]
    label: str = Field(description="Short label, e.g. 'FROM ROAD MILK TANKER'; empty for headers/tees")


class IRConnection(BaseModel):
    source: str = Field(description="Equipment tag or node id the pipe leaves")
    target: str = Field(description="Equipment tag or node id the pipe enters")
    kind: Literal["process", "bypass"]


class IRValve(BaseModel):
    on: str = Field(description="Connection as 'SOURCE>TARGET'")
    type: Literal["butterfly_valve", "check_valve", "on_off_valve", "globe_valve"] = Field(
        description="Valve type the cited line rule requires")
    rule: str = Field(description="Id of the line rule that requires it, e.g. 'LR-PUMP-SUCTION'")


class IRInstrument(BaseModel):
    function: str = Field(description="ISA letters, e.g. LT, TT, FT, PI, PDI, PT, LSHH, TSL, FQI")
    on: str = Field(description="Equipment tag, or a connection 'SOURCE>TARGET' for line instruments")
    rule: str = Field(description="Id of the instrumentation rule that requires it, e.g. 'IR-SILO-LEVEL'")
    alarms: list[str] = Field(description="Alarm letters from the rule, e.g. ['LAH','LAL']; empty if none")
    inline_element: bool = Field(description="True for an in-line primary element (flow meter on the pipe)")


class IRLoop(BaseModel):
    """A control loop the rule requires. Controller, measuring instrument, final control element and
    setpoint all follow from the rule and the plan's connections — the system derives them."""

    rule: str = Field(description="Id of the instrumentation rule whose loop this is, e.g. 'IR-CHILLER-TEMP'")
    equipment: str = Field(description="Tag of the equipment the loop controls, e.g. 'E-101'")


class PIDPlan(BaseModel):
    nodes: list[IRNode]
    connections: list[IRConnection]
    valves: list[IRValve]
    instruments: list[IRInstrument]
    loops: list[IRLoop]
    notes: list[str] = Field(description="Assumptions or choices worth telling the reviewer; empty if none")


# ---- corrections: after a failed validation the planner returns a patch, not a whole new plan ----------
# A patch keeps everything that already passed untouched (a full rewrite tends to copy the old plan and
# silently drop or undo fixes) and makes the planner account for every error it was given.


class ErrorFix(BaseModel):
    error: int = Field(description="Number of the error in the list you were given")
    action: str = Field(description="What you change to fix it, in a few words")


class IRInstrumentRef(BaseModel):
    function: str
    on: str


class PlanPatch(BaseModel):
    fixes: list[ErrorFix] = Field(description="One entry per error number, in order — how this patch fixes it")
    add_nodes: list[IRNode]
    remove_nodes: list[str] = Field(description="Node ids to delete (their connections, valves and instruments go too)")
    add_connections: list[IRConnection]
    remove_connections: list[str] = Field(description="Connections 'SOURCE>TARGET' to delete (with their valves and instruments)")
    add_valves: list[IRValve]
    remove_valves: list[IRValve] = Field(description="Exact valve entries to delete")
    add_instruments: list[IRInstrument]
    remove_instruments: list[IRInstrumentRef]
    add_loops: list[IRLoop]
    remove_loops: list[IRLoop]


def _norm(ref: str) -> str:
    return ref.replace(" ", "")


def apply_patch(plan: PIDPlan, patch: PlanPatch) -> tuple[PIDPlan, list[str], str]:
    """Apply a patch deterministically. Returns (new plan, problems, one-line summary).

    Problems are removals that match nothing; they go back to the planner with the next errors."""
    p = plan.model_copy(deep=True)
    problems: list[str] = []

    gone_nodes = {n for n in patch.remove_nodes}
    for n in gone_nodes - {x.id for x in p.nodes}:
        problems.append(f"remove_nodes: no node '{n}' in the plan")
    p.nodes = [n for n in p.nodes if n.id not in gone_nodes]

    gone_conn = {_norm(c) for c in patch.remove_connections}
    have = {f"{c.source}>{c.target}" for c in p.connections}
    for c in gone_conn - have:
        problems.append(f"remove_connections: no connection '{c}' in the plan")
    gone_conn |= {c for c in have if c.split(">")[0] in gone_nodes or c.split(">")[1] in gone_nodes}
    p.connections = [c for c in p.connections if f"{c.source}>{c.target}" not in gone_conn]

    def removed(key, entries, refs, label):
        """Each removal deletes ONE matching entry (so 'remove the duplicate' keeps the other copy)."""
        kept = list(entries)
        for w in (key(r) for r in refs):
            hit = next((k for k, e in enumerate(kept) if key(e) == w), None)
            if hit is None:
                problems.append(f"remove_{label}: no {label[:-1]} {' / '.join(w)} in the plan")
            else:
                kept.pop(hit)
        return kept

    def added(key, entries, new, label):
        """Adding something already in the plan does not add it twice."""
        out = list(entries)
        for x in new:
            if any(key(e) == key(x) for e in out):
                problems.append(f"add_{label}: {' / '.join(key(x))} is already in the plan — not added again")
            else:
                out.append(x)
        return out

    p.valves = removed(lambda v: (_norm(v.on), v.type, v.rule), p.valves, patch.remove_valves, "valves")
    p.instruments = removed(lambda i: (i.function, _norm(i.on)), p.instruments, patch.remove_instruments, "instruments")
    p.loops = removed(lambda lp: (lp.rule, lp.equipment.strip()), p.loops, patch.remove_loops, "loops")
    # items that sat on a deleted connection or node go with it
    p.valves = [v for v in p.valves if _norm(v.on) not in gone_conn]
    p.instruments = [i for i in p.instruments if _norm(i.on) not in gone_conn and i.on not in gone_nodes]

    p.nodes = added(lambda n: (n.id,), p.nodes, patch.add_nodes, "nodes")
    p.connections = added(lambda c: (_norm(c.source), _norm(c.target)), p.connections, patch.add_connections, "connections")
    p.valves = added(lambda v: (_norm(v.on), v.type, v.rule), p.valves, patch.add_valves, "valves")
    p.instruments = added(lambda i: (i.function, _norm(i.on)), p.instruments, patch.add_instruments, "instruments")
    p.loops = added(lambda lp: (lp.rule, lp.equipment.strip()), p.loops, patch.add_loops, "loops")

    parts = [f"{verb} {len(xs)} {what}" for verb, what, xs in (
        ("+", "nodes", patch.add_nodes), ("−", "nodes", patch.remove_nodes),
        ("+", "connections", patch.add_connections), ("−", "connections", patch.remove_connections),
        ("+", "valves", patch.add_valves), ("−", "valves", patch.remove_valves),
        ("+", "instruments", patch.add_instruments), ("−", "instruments", patch.remove_instruments),
        ("+", "loops", patch.add_loops), ("−", "loops", patch.remove_loops),
    ) if xs]
    return p, problems, (", ".join(parts) or "no changes")
