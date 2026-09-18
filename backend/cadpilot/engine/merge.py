"""Merge engine: reconcile worker proposals into one engineering model, resolving conflicts
and applying the reviewer's persisted design intent."""

from __future__ import annotations

from ..model.engineering import (
    Decision,
    EngineeringModel,
    Equipment,
    ProcessDefinition,
    ProjectInfo,
    Quantity,
)
from ..model.intent import DesignIntent
from ..model.proposals import (
    EquipmentProposal,
    InstrumentationProposal,
    ProcessProposal,
    TopologyProposal,
)


def reconcile(
    process: ProcessProposal, equipment: EquipmentProposal, intent: DesignIntent
) -> tuple[ProcessDefinition, list[Equipment], list[Decision]]:
    """Primary-agent decision between Process and Equipment proposals (before topology)."""
    decisions: list[Decision] = []
    proc = process.process.model_copy(deep=True)
    for g, n in equipment.group_counts.items():
        if proc.groups.get(g) != n:
            decisions.append(
                Decision(
                    agent="primary_agent",
                    summary=f"{g} group: {n} parallel unit(s)",
                    detail=f"Design data quantity ({n}) takes precedence over template default ({proc.groups.get(g)})",
                )
            )
        proc.groups[g] = n

    stage_ids = {s.id for s in proc.stages}
    kept: list[Equipment] = []
    dropped: dict[str, int] = {}
    for eq in equipment.equipment:
        if eq.stage in stage_ids:
            kept.append(eq.model_copy(deep=True))
        else:
            dropped[eq.name] = dropped.get(eq.name, 0) + 1
    for name, n in dropped.items():
        decisions.append(
            Decision(
                agent="primary_agent",
                summary=f"Excluded {n}× {name}",
                detail="Equipment Agent found it in the design data, but the process definition excludes that stage",
            )
        )

    for eq in kept:
        ov = intent.equipment_overrides.get(eq.tag)
        if not ov:
            continue
        if ov.capacity:
            eq.capacity = Quantity(**ov.capacity.model_dump())
            eq.provenance.rationale += f" · capacity set to {ov.capacity} by reviewer"
        if ov.name:
            eq.name = ov.name
        eq.attributes.update(ov.attributes)
    return proc, kept, decisions


def assemble(
    project: ProjectInfo,
    process: ProcessDefinition,
    process_prop: ProcessProposal,
    equipment: list[Equipment],
    topology: TopologyProposal,
    instr: InstrumentationProposal,
    intent: DesignIntent,
    decisions: list[Decision],
) -> EngineeringModel:
    lines = [ln.model_copy(deep=True) for ln in topology.lines]
    for ln in lines:
        ln.inline = sorted(ln.inline + instr.inline.get(ln.tag, []), key=lambda c: c.position)

    instruments = [i.model_copy(deep=True) for i in instr.instruments]
    loops = [lp.model_copy(deep=True) for lp in instr.loops]

    # Reviewer suppressions (rule waivers) — removed from the model, recorded in metadata
    waived: dict[str, dict] = {}
    for tag, reason in intent.suppressed.items():
        inst = next((i for i in instruments if i.tag == tag), None)
        comp = next(((ln, c) for ln in lines for c in ln.inline if c.tag == tag), None)
        if inst:
            waived[tag] = {"reason": reason, "rule": inst.provenance.rule, "function": inst.function, "ref": inst.attached_to.ref}
        elif comp:
            waived[tag] = {"reason": reason, "rule": comp[1].provenance.rule, "function": comp[1].type, "ref": comp[0].tag}
        else:
            waived[tag] = {"reason": reason, "rule": None, "function": None, "ref": None}
    if waived:
        instruments = [i for i in instruments if i.tag not in waived]
        for ln in lines:
            ln.inline = [c for c in ln.inline if c.tag not in waived]
        loops = [lp for lp in loops if lp.tag not in waived and lp.final_element not in waived and lp.measured_by not in waived]
        live_loops = {lp.tag for lp in loops}
        for i in instruments:
            if i.loop and i.loop not in live_loops:
                i.loop = None

    streams = [s.model_copy(deep=True) for s in process_prop.streams]
    for s in streams:
        flows = [ln.design_flow.value for ln in lines if ln.service == s.service and ln.design_flow and ln.kind == "process"]
        if flows:
            s.design_flow = Quantity(value=max(flows), unit="KLPH")

    return EngineeringModel(
        project=project,
        process=process,
        equipment=equipment,
        piping_nodes=[n.model_copy(deep=True) for n in topology.piping_nodes],
        streams=streams,
        lines=lines,
        instruments=instruments,
        loops=loops,
        metadata={
            "decisions": [d.model_dump() for d in decisions],
            "waivers": waived,
            "assumptions": process_prop.assumptions,
        },
    )
