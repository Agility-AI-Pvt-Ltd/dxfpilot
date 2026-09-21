"""Design flow of every process line — shared by the rules topology agent and the IR compiler.

Each train's flow is set by its flow-driving stage (the pump); groups without one fall back to the
largest flow-rated unit in the group. A manifold carries the sum of what enters it, a manifold
branch carries what the unit it feeds draws, and a volume unit (silo) supplies the total demand
downstream of it.
"""

from __future__ import annotations

from ..model.engineering import Equipment
from .sizing import flow_m3h


class LineFlows:
    def __init__(
        self,
        edges: list[tuple[str, str]],  # process edges (bypasses excluded)
        kinds: dict[str, str],  # tag -> equipment type / piping-node kind
        group_of: dict[str, str],  # tag -> train group
        train_of: dict[str, int | None],
        equipment: list[Equipment],
        template: dict,
        links: list[tuple[str, str]] = (),  # outlet terminal → inlet terminal of another module
    ):
        self.edges = edges
        self.links = list(links)
        self.kinds = kinds
        self.group_of = group_of
        self.train_of = train_of
        # a flow-rated unit's capacity; a machine rated otherwise (pouches/h) may declare its liquid flow
        cap = {e.tag: flow_m3h(e.capacity) or _num(e.attributes.get("design_flow_klph")) for e in equipment}
        self.group_flow: dict[str, float] = {}
        for e in equipment:
            g = group_of.get(e.tag)
            if g and cap.get(e.tag):
                self.group_flow[g] = max(self.group_flow.get(g, 0), cap[e.tag])  # type: ignore[arg-type]
        self.train_flow: dict[tuple[str, int | None], float] = {}
        for g, gcfg in template["groups"].items():
            for e in equipment:
                if e.stage == gcfg.get("flow_stage") and cap.get(e.tag):
                    self.train_flow[(g, e.train)] = cap[e.tag]  # type: ignore[assignment]

    def unit_flow(self, tag: str) -> float | None:
        g = self.group_of.get(tag, "")
        return self.train_flow.get((g, self.train_of.get(tag))) or self.group_flow.get(g)

    def flow_from(self, tag: str, seen: frozenset = frozenset()) -> float | None:
        if tag in seen:
            return None
        kind = self.kinds.get(tag)
        if kind in ("header", "tee"):
            srcs = [u for (u, d) in self.edges if d == tag]
            ins = [f for u in srcs for f in [self.flow_from(u, seen | {tag})] if f]
            # tanks emptying into a header do so one at a time; running units add up
            if kind == "tee" or (srcs and all(self.kinds.get(u) not in ("header", "tee", "terminal_in") and not self.unit_flow(u) for u in srcs)):
                return max(ins) if ins else None
            return sum(ins) if ins else None
        if kind == "terminal_in" or (self.unit_flow(tag) and self.group_of.get(tag) in self.group_flow):
            # an inlet battery limit without its own flow carries what the units after it draw
            return self.unit_flow(tag) or (self.demand(tag) if kind == "terminal_in" else None)
        # a tank supplies what is drawn after it; if nothing downstream is rated, what flows into it
        return self.demand(tag) or self.inflow(tag, seen | {tag})

    def inflow(self, tag: str, seen: frozenset = frozenset()) -> float | None:
        ins = [f for (u, d) in self.edges if d == tag and u not in seen for f in [self.line_flow(u, tag, seen)] if f]
        return max(ins) if ins else None

    def demand(self, tag: str, seen: frozenset = frozenset()) -> float | None:
        """What the units downstream draw: through headers, and across a module interface
        (an outlet battery limit takes the demand of the module it feeds)."""
        if tag in seen:
            return None
        running = 0.0  # units drawing at the same time (parallel trains): their flows add up
        storage = 0.0  # tanks behind a header are filled one at a time: the largest demand counts
        nxt = [d for (u, d) in self.edges if u == tag] + [d for (u, d) in self.links if u == tag]
        for d in nxt:
            if self.kinds.get(d) in ("header", "tee", "terminal_out", "terminal_in"):
                running += self.demand(d, seen | {tag}) or 0
            elif self.unit_flow(d):
                running += self.unit_flow(d) or 0
            elif self.kinds.get(tag) == "header":
                storage = max(storage, self.demand(d, seen | {tag}) or 0)
            else:
                running += self.demand(d, seen | {tag}) or 0
        return (running + storage) or None

    def line_flow(self, u: str, d: str, seen: frozenset = frozenset()) -> float | None:
        f = self.flow_from(u, seen)
        if self.kinds.get(u) == "header" and self.unit_flow(d):
            f = self.unit_flow(d)  # a manifold branch carries what the train it feeds draws
        elif f and self.kinds.get(u) == "header" and self.kinds.get(d) == "terminal_out":
            # utility take-offs whose consumer demand is not known share the header flow equally
            outs = [x for (h, x) in self.edges if h == u and self.kinds.get(x) == "terminal_out"]
            f = f / len(outs)
        return f


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
