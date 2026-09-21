"""Traceability — for any item on the drawing, the chain from the value back to its source.

    Pasteurizer E-201
      capacity 20 KLPH  ← Dairy Plant Estimation.xlsx › Eqpt. Cost Est. › row 20 "Milk Pasteurizer …"
      2 units           ← 25 KLPH required (500,000 L/day ÷ 20 h) ÷ 20 KLPH per unit = ⌈1.25⌉
      basis             ← plant capacity 500,000 L/day, approved by the reviewer (Mass balance › Sheet1 › row 4)

Nothing here is re-decided: every step reads what the pipeline recorded when it produced the item.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..model.engineering import EngineeringModel
from . import line_rules
from .sizing import flow_m3h


class SourceRef(BaseModel):
    file: str
    sheet: str
    row: int | None = None
    text: str = ""


class Step(BaseModel):
    label: str  # "Capacity", "Quantity", "Calculation", "Design basis", "Source", "Rule" …
    value: str
    detail: str = ""
    source: SourceRef | None = None


class Trace(BaseModel):
    tag: str
    kind: str  # equipment | line | instrument | node
    title: str
    steps: list[Step]


def parse_ref(ref: str | None, text: str = "") -> SourceRef | None:
    """'file.xlsx:Sheet!R12' → file, sheet, row."""
    if not ref or "!" not in ref:
        return None
    left, cell = ref.rsplit("!", 1)
    file, _, sheet = left.rpartition(":")
    row = int(cell[1:]) if cell[:1] == "R" and cell[1:].isdigit() else None
    return SourceRef(file=file or left, sheet=sheet or left, row=row, text=text)


def _row_text(source, ref: str | None) -> str:
    if source is None or not ref:
        return ""
    row = next((r for r in source.equipment_rows if r.ref == ref), None)
    if row is None:
        return ""
    cells = [row.description.splitlines()[0]]
    if row.capacity_raw:
        cells.append(f"capacity cell: {row.capacity_raw}")
    if row.qty is not None:
        cells.append(f"qty: {row.qty:g}")
    return " · ".join(cells)


def _basis_steps(used: list[dict]) -> list[Step]:
    out = []
    for b in used:
        how = {"approved": "approved by the reviewer", "agreed": "all sources agree", "single_source": "only one source"}.get(b["status"], b["status"])
        out.append(Step(label="Design basis", value=f"{b['name']}: {b['value']:,.0f} {b['unit']}", detail=f"{how} — {b['source']}",
                        source=parse_ref(b.get("ref"))))
    return out


def trace(model: EngineeringModel, tag: str, source=None, standards=None) -> Trace | None:
    sizing = {r["stage"]: r for r in model.metadata.get("sizing", [])}
    eq = model.get_equipment(tag)
    if eq is not None:
        steps = [Step(label="Equipment", value=f"{eq.tag} — {eq.name}", detail=f"{eq.type.replace('_', ' ')} · stage {eq.stage} · area {eq.area}")]
        prov = eq.provenance
        if eq.capacity is not None:
            if prov.source and "!" in prov.source:
                steps.append(Step(label="Capacity", value=str(eq.capacity), detail="from the design data",
                                  source=parse_ref(prov.source, _row_text(source, prov.source))))
            else:
                steps.append(Step(label="Capacity", value=str(eq.capacity), detail="catalogue default of the engineering module "
                                  "(no design-data row) — an assumption"))
        if "by reviewer" in prov.rationale:
            steps.append(Step(label="Changed by reviewer", value=prov.rationale.split("·")[-1].strip()))
        rec = sizing.get(eq.stage)
        n = sum(1 for e in model.equipment if e.stage == eq.stage)
        if rec and rec.get("units"):
            steps.append(Step(label="Quantity", value=f"{n} unit(s) — this is unit {eq.train or 1} of {n}",
                              detail="calculated by the equipment selection engine, not chosen by the AI"))
            standby = f" + {rec['standby']} standby" if rec["standby"] else ""
            steps.append(Step(label="Calculation", value=f"{rec['required']} ÷ {rec['unit_capacity']} = {rec['ratio']} → ⌈{rec['ratio']}⌉ = {rec['working']}{standby}",
                              detail=f"required: {rec['basis']}" + (f" · {rec['note']}" if rec.get("note") else "")))
            steps += _basis_steps(rec.get("basis_used", []))
            if rec.get("design_data_qty"):
                same = rec["design_data_qty"] == rec["units"]
                steps.append(Step(label="Design data lists", value=f"{rec['design_data_qty']} unit(s)",
                                  detail="agrees with the calculation" if same else "DIFFERS — the drawing follows the mass balance",
                                  source=parse_ref(rec.get("source_ref"), _row_text(source, rec.get("source_ref")))))
        elif rec:
            steps.append(Step(label="Quantity", value=f"{n} unit(s)", detail=rec.get("note", "")))
        else:
            steps.append(Step(label="Quantity", value=f"{n} unit(s)", detail="from the design-data quantity or the template "
                              "(no mass-balance basis for this stage)"))
        steps.append(Step(label="Selected by", value=prov.agent.replace("_", " "), detail=prov.rationale))
        for i in model.instruments:
            if i.attached_to.ref == eq.tag and i.provenance.rule:
                steps.append(Step(label="Instrument", value=f"{i.tag} ({i.function})", detail=f"rule {i.provenance.rule} — {i.provenance.rationale}"))
        return Trace(tag=tag, kind="equipment", title=eq.name, steps=steps)

    ln = model.get_line(tag)
    if ln is not None:
        steps = [Step(label="Line", value=f"{ln.tag}: {ln.from_.item} → {ln.to.item}", detail=f"service {ln.service} · {ln.kind}")]
        if ln.design_flow:
            src = model.get_equipment(ln.from_.item)
            why = (f"the flow of the train it belongs to — set by the capacity of its pump / driving unit"
                   if src is not None else "the sum of the flows entering the header / battery limit")
            steps.append(Step(label="Design flow", value=str(ln.design_flow), detail=why))
        if ln.size_dn and ln.design_flow and standards is not None and (f := flow_m3h(ln.design_flow)) is not None:
            v, vmax = line_rules.line_velocity(standards.line_rules, ln.service, f, ln.size_dn)
            target = line_rules.service_sizing(standards.line_rules, ln.service)[0]
            steps.append(Step(label="Pipe size", value=f"DN{ln.size_dn}",
                              detail=f"smallest standard size at or below {target:g} m/s: {v:.2f} m/s at the design flow (max {vmax:g} m/s)"))
        if ln.spec:
            steps.append(Step(label="Piping spec", value=ln.spec))
        for comp in ln.inline:
            steps.append(Step(label="Inline item", value=f"{comp.tag} ({comp.type.replace('_', ' ')})",
                              detail=f"rule {comp.provenance.rule} — {comp.provenance.rationale}" if comp.provenance.rule else comp.provenance.rationale))
        return Trace(tag=tag, kind="line", title=f"Line {ln.tag}", steps=steps)

    inst = model.get_instrument(tag)
    if inst is not None:
        steps = [Step(label="Instrument", value=f"{inst.tag} — {inst.function}", detail=f"{inst.location} · on {inst.attached_to.kind} {inst.attached_to.ref}")]
        if inst.provenance.rule:
            steps.append(Step(label="Rule", value=inst.provenance.rule, detail=inst.provenance.rationale))
        if inst.alarms:
            steps.append(Step(label="Alarms", value=", ".join(inst.alarms)))
        loop = next((lp for lp in model.loops if lp.tag == inst.loop), None)
        if loop:
            steps.append(Step(label="Control loop", value=f"{loop.tag}: {loop.measured_by} → {loop.final_element}",
                              detail=f"setpoint {loop.setpoint}" if loop.setpoint else ""))
        steps.append(Step(label="Proposed by", value=inst.provenance.agent.replace("_", " ")))
        return Trace(tag=tag, kind="instrument", title=f"{inst.function} {inst.tag}", steps=steps)

    node = next((n for n in model.piping_nodes if n.tag == tag), None)
    if node is not None:
        return Trace(tag=tag, kind="node", title=node.label or node.kind, steps=[
            Step(label=node.kind.replace("_", " ").title(), value=node.tag, detail=node.label),
            Step(label="Why", value=node.provenance.rule or node.provenance.agent.replace("_", " "), detail=node.provenance.rationale),
        ])
    return None
