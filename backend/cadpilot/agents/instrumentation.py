"""Instrumentation Agent — measurement, control loops and final control elements from the
configured instrumentation rules, plus instruments the reviewer asked for."""

from __future__ import annotations

from ..model.engineering import (
    ControlLoop,
    Equipment,
    InlineComponent,
    Instrument,
    InstrumentAttachment,
    Line,
    PipingNode,
    Provenance,
)
from ..model.proposals import InstrumentationProposal
from .context import AgentContext

AGENT = "instrumentation_agent"


def rule_applies(rule: dict, eq: Equipment) -> bool:
    a = rule["applies_to"]
    return eq.type == a.get("equipment_type") or eq.stage == a.get("stage")


def attachment_line(rule_attach: str, eq: Equipment, lines: list[Line]) -> Line | None:
    if rule_attach == "inlet_line":
        return next((ln for ln in lines if ln.to.item == eq.tag and ln.kind != "bypass"), None)
    if rule_attach == "outlet_line":
        return next((ln for ln in lines if ln.from_.item == eq.tag and ln.kind != "bypass"), None)
    return None


def _walk_to_pump(start: str, lines: list[Line], kinds: dict[str, str], upstream: bool) -> str | None:
    """Follow the single process path until a pump is found (stops at manifolds)."""
    current, seen = start, set()
    while current not in seen:
        seen.add(current)
        nxt = [
            (ln.from_.item if upstream else ln.to.item)
            for ln in lines
            if ln.kind != "bypass" and (ln.to.item if upstream else ln.from_.item) == current
        ]
        if len(nxt) != 1:
            return None
        current = nxt[0]
        if kinds.get(current) == "centrifugal_pump":
            return current
        if kinds.get(current) == "header":
            return None
    return None


def run(
    ctx: AgentContext,
    equipment: list[Equipment],
    nodes: list[PipingNode],
    lines: list[Line],
) -> InstrumentationProposal:
    tagger = ctx.tagger
    cfg = ctx.standards.instrumentation
    symbols = cfg["symbols"]
    kinds = {e.tag: e.type for e in equipment} | {n.tag: n.kind for n in nodes}
    instruments: list[Instrument] = []
    loops: list[ControlLoop] = []
    inline: dict[str, list[InlineComponent]] = {}
    warnings: list[str] = []

    for rule in cfg["rules"]:
        for eq in equipment:
            if not rule_applies(rule, eq):
                continue
            prov = Provenance(agent=AGENT, rule=rule["id"], rationale=rule["rationale"])
            line = attachment_line(rule["attach"], eq, lines)
            if rule["attach"] != "equipment" and line is None:
                warnings.append(f"{rule['id']}: {eq.tag} has no {rule['attach'].replace('_', ' ')}")
                continue
            attach = (
                InstrumentAttachment(kind="equipment", ref=eq.tag)
                if line is None
                else InstrumentAttachment(kind="line", ref=line.tag)
            )
            loop_no = tagger.loop_number(f"{rule['id']}:{eq.stage}#{eq.train}", eq.area)
            created: list[Instrument] = []
            for spec in rule["instruments"]:
                location = spec.get("location", "field")
                inst = Instrument(
                    tag=tagger.instrument(spec["function"], loop_no),
                    function=spec["function"],
                    type=symbols[location],
                    location=location,
                    attached_to=attach,
                    alarms=list(spec.get("alarms", [])),
                    provenance=prov,
                )
                created.append(inst)
                if spec.get("inline") and line is not None:
                    inline.setdefault(line.tag, []).append(
                        InlineComponent(tag=f"FE-{loop_no}", type=spec["inline"], position=0.6, provenance=prov)
                    )
            loop_cfg = rule.get("loop")
            if loop_cfg:
                ctrl_tag = f"{loop_cfg['controller']}-{loop_no}"
                final_kind = loop_cfg["final_element"]
                final_tag: str | None = None
                kind = "valve"
                if final_kind == "utility_valve":
                    final_tag = f"{loop_cfg['controller'][0]}CV-{loop_no}"
                    created.append(
                        Instrument(
                            tag=final_tag,
                            function=f"{loop_cfg['controller'][0]}CV",
                            type=symbols["utility_valve"],
                            attached_to=InstrumentAttachment(kind="equipment", ref=eq.tag, port="utility"),
                            loop=ctrl_tag,
                            provenance=prov,
                        )
                    )
                elif final_kind == "outlet_diversion_valve":
                    out_line = attachment_line("outlet_line", eq, lines)
                    if out_line:
                        final_tag = f"FDV-{loop_no}"
                        inline.setdefault(out_line.tag, []).append(
                            InlineComponent(tag=final_tag, type=symbols["diversion_valve"], position=0.35, provenance=prov)
                        )
                elif final_kind in ("upstream_pump_vfd", "downstream_pump_vfd"):
                    final_tag = _walk_to_pump(eq.tag, lines, kinds, upstream=final_kind.startswith("up"))
                    kind = "vfd"
                if final_tag is None:
                    warnings.append(f"{rule['id']}: no final control element found for {eq.tag}")
                else:
                    if not any(i.tag == ctrl_tag for i in created):
                        created.append(
                            Instrument(
                                tag=ctrl_tag,
                                function=loop_cfg["controller"],
                                type=symbols["dcs"],
                                location="dcs",
                                attached_to=attach,
                                provenance=prov,
                            )
                        )
                    loops.append(
                        ControlLoop(
                            tag=ctrl_tag,
                            measured_by=created[0].tag,
                            final_element=final_tag,
                            final_element_kind=kind,  # type: ignore[arg-type]
                            setpoint=loop_cfg.get("setpoint", ""),
                            provenance=prov,
                        )
                    )
                    for i in created:
                        i.loop = ctrl_tag
            instruments += created

    # Reviewer-requested instruments (persisted design intent)
    line_area = {ln.tag: ln.tag.split("-")[-1][:1] for ln in lines}
    eq_area = {e.tag: e.area for e in equipment}
    for add in ctx.intent.added_instruments:
        area = eq_area.get(add.attached_ref) or line_area.get(add.attached_ref)
        if area is None:
            warnings.append(f"Requested {add.function} on {add.attached_ref}: item no longer exists")
            continue
        loop_no = tagger.loop_number(f"added:{add.function}:{add.attached_ref}", area)
        location = "dcs" if add.function.endswith(("IC", "C")) and len(add.function) >= 3 else "field"
        instruments.append(
            Instrument(
                tag=tagger.instrument(add.function, loop_no),
                function=add.function,
                type=symbols[location],
                location=location,  # type: ignore[arg-type]
                attached_to=InstrumentAttachment(kind=add.attached_kind, ref=add.attached_ref),
                provenance=Provenance(agent="reviewer", rule="REVIEWER", rationale=add.rationale),
            )
        )

    return InstrumentationProposal(instruments=instruments, loops=loops, inline=inline, warnings=warnings)
