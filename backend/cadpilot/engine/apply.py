"""Apply structured change operations to the design intent (deterministic).

Returns human-readable notes for each applied op, and rejections with the reason when an op
cannot be applied (e.g. removing a mandatory stage)."""

from __future__ import annotations

import re

from ..model.engineering import EngineeringModel, Quantity
from ..model.intent import (
    AddBypass,
    AddedInstrument,
    AddInstrument,
    ChangeOp,
    DesignIntent,
    EquipmentOverride,
    Explain,
    MoveItem,
    RemoveBypass,
    RemoveItem,
    ReorderStage,
    SetAttribute,
    SetCapacity,
    SetGroupCount,
    ToggleStage,
)
from ..standards import Standards

CAPACITY_UNITS = {
    "klph": "KLPH", "kl/h": "KLPH", "lph": "LPH", "l/h": "LPH", "m3/h": "m3/h", "m³/h": "m3/h",
    "kl": "KL", "l": "L", "ltr": "L", "litre": "L", "liter": "L", "kg/h": "kg/h", "kg/hr": "kg/h",
}


def _stem(word: str) -> str:
    w = re.sub(r"[^a-z0-9 ]", " ", word.lower()).strip()
    for suffix in ("ation", "ization", "ers", "er", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[: -len(suffix)]
    return w


def resolve_stage(term: str, model: EngineeringModel) -> str | None:
    """Map a tag, stage id or plain-language name ('the separator') to a process stage id."""
    t = term.strip().strip(".").upper()
    eq = model.get_equipment(t)
    if eq:
        return eq.stage
    low = re.sub(r"^(the|a|an)\s+", "", term.strip().strip(".").lower())
    for st in model.process.stages:
        if low == st.id:
            return st.id
    words = [_stem(w) for w in low.split() if len(w) > 2]
    best, best_score = None, 0
    for st in model.process.stages:
        hay = f"{st.id.replace('_', ' ')} {st.name.lower()} {(st.equipment_type or '').replace('_', ' ')}"
        score = sum(1 for w in words if w and w in hay)
        if score > best_score:
            best, best_score = st.id, score
    return best


def resolve_tags(term: str, model: EngineeringModel) -> list[str]:
    t = term.strip().strip(".").upper()
    if model.find(t):
        return [t]
    stage = resolve_stage(term, model)
    return [e.tag for e in model.equipment if e.stage == stage] if stage else []


def explain(tag: str, model: EngineeringModel) -> str:
    found = model.find(tag)
    if not found:
        return f"I can't find {tag} in the current model."
    kind, obj = found
    prov = getattr(obj, "provenance", None)
    lines = []
    if kind == "equipment":
        lines.append(f"{obj.tag} — {obj.name} ({obj.capacity or 'no capacity'}).")  # type: ignore[attr-defined]
    elif kind == "instrument":
        fn = obj.function  # type: ignore[attr-defined]
        lines.append(f"{obj.tag} — {fn} on {obj.attached_to.ref}.")  # type: ignore[attr-defined]
        if obj.loop:  # type: ignore[attr-defined]
            lp = next((lp for lp in model.loops if lp.tag == obj.loop), None)  # type: ignore[attr-defined]
            if lp:
                lines.append(f"Part of loop {lp.tag}: {lp.measured_by} → {lp.tag} → {lp.final_element} ({lp.final_element_kind}), setpoint {lp.setpoint or 'n/a'}.")
    elif kind == "line":
        lines.append(f"{obj.tag} — {obj.from_} → {obj.to}, DN{obj.size_dn}, {obj.design_flow}.")  # type: ignore[attr-defined]
    elif kind == "inline":
        lines.append(f"{obj.tag} — {obj.type.replace('_', ' ')}.")  # type: ignore[attr-defined]
    if prov:
        why = prov.rationale or "no rationale recorded"
        rule = f" (rule {prov.rule})" if prov.rule else ""
        src = f" Source: {prov.source}." if prov.source else ""
        lines.append(f"Why: {why}{rule}. Proposed by {prov.agent.replace('_', ' ')}.{src}")
    return " ".join(lines)


def apply_ops(
    intent: DesignIntent, ops: list[ChangeOp], model: EngineeringModel, standards: Standards
) -> tuple[DesignIntent, list[str], list[str], list[str]]:
    """Returns (new_intent, applied_notes, rejected_notes, answers)."""
    new = intent.model_copy(deep=True)
    applied: list[str] = []
    rejected: list[str] = []
    answers: list[str] = []
    tpl = standards.template(new.template)
    optional = {s["id"] for s in tpl["stages"] if s.get("optional")}

    for op in ops:
        if isinstance(op, Explain):
            answers.append(explain(op.tag, model))

        elif isinstance(op, ReorderStage):
            stage = resolve_stage(op.object, model)
            anchor_term = op.before or op.after
            anchor = resolve_stage(anchor_term, model) if anchor_term else None
            if not stage or not anchor or stage == anchor:
                rejected.append(f"Could not identify the stages in '{op.object}' / '{anchor_term}'.")
                continue
            order = [s.id for s in model.process.stages]
            order.remove(stage)
            idx = order.index(anchor) + (0 if op.before else 1)
            order.insert(idx, stage)
            new.stage_order = order
            applied.append(f"Moved stage '{stage}' {'before' if op.before else 'after'} '{anchor}'.")

        elif isinstance(op, SetCapacity):
            tags = resolve_tags(op.tag, model)
            tags = [t for t in tags if model.get_equipment(t)]
            unit = CAPACITY_UNITS.get(op.unit.strip().lower().replace(" ", ""))
            if not tags:
                rejected.append(f"No equipment matches '{op.tag}'.")
                continue
            if unit is None or op.value <= 0:
                rejected.append(
                    f"'{op.value:g} {op.unit}' is not a capacity (use {', '.join(sorted(set(CAPACITY_UNITS.values())))})."
                )
                continue
            for t in tags:
                ov = new.equipment_overrides.get(t, EquipmentOverride())
                ov.capacity = Quantity(value=op.value, unit=unit)
                new.equipment_overrides[t] = ov
            applied.append(f"Set capacity of {', '.join(tags)} to {op.value:g} {unit}.")

        elif isinstance(op, SetAttribute):
            tags = [t for t in resolve_tags(op.tag, model) if model.get_equipment(t)]
            if not tags:
                rejected.append(f"No equipment matches '{op.tag}'.")
                continue
            for t in tags:
                ov = new.equipment_overrides.get(t, EquipmentOverride())
                if op.field == "name":
                    ov.name = op.value
                else:
                    ov.attributes[op.field] = op.value
                new.equipment_overrides[t] = ov
            applied.append(f"Set {op.field} of {', '.join(tags)} to {op.value}.")

        elif isinstance(op, SetGroupCount):
            groups = tpl["groups"]
            g = op.group if op.group in groups else next(
                (k for k, v in groups.items() if resolve_stage(op.group, model) == v["lead_stage"]), None
            )
            if not g or op.count < 1:
                rejected.append(f"Unknown train group '{op.group}'.")
                continue
            new.group_counts[g] = op.count
            applied.append(f"Set {g} group to {op.count} parallel unit(s).")

        elif isinstance(op, AddBypass):
            tags = [t for t in resolve_tags(op.around, model) if model.get_equipment(t)]
            if not tags:
                rejected.append(f"No equipment matches '{op.around}' for a bypass.")
                continue
            for t in tags:
                if t not in new.bypasses:
                    new.bypasses.append(t)
            applied.append(f"Added bypass around {', '.join(tags)}.")

        elif isinstance(op, RemoveBypass):
            tags = resolve_tags(op.around, model)
            removed = [t for t in tags if t in new.bypasses]
            new.bypasses = [t for t in new.bypasses if t not in removed]
            (applied if removed else rejected).append(
                f"Removed bypass around {', '.join(removed)}." if removed else f"No bypass exists around '{op.around}'."
            )

        elif isinstance(op, ToggleStage):
            stage = resolve_stage(op.stage, model) if op.op == "disable_stage" else (
                next((s["id"] for s in tpl["stages"] if s["id"] == op.stage), None)
                or next((s["id"] for s in tpl["stages"] if _stem(op.stage) in s["name"].lower() or _stem(op.stage) in s["id"]), None)
            )
            if not stage:
                rejected.append(f"Unknown stage '{op.stage}'.")
                continue
            if op.op == "disable_stage":
                if stage not in optional:
                    rejected.append(f"'{stage}' is a required process stage and cannot be removed.")
                    continue
                new.disabled_stages = sorted(set(new.disabled_stages) | {stage})
                new.enabled_stages = [s for s in new.enabled_stages if s != stage]
                applied.append(f"Removed optional stage '{stage}'.")
            else:
                new.disabled_stages = [s for s in new.disabled_stages if s != stage]
                if stage in optional:
                    new.enabled_stages = sorted(set(new.enabled_stages) | {stage})
                applied.append(f"Included stage '{stage}'.")

        elif isinstance(op, AddInstrument):
            ref = op.attached_to.strip().upper()
            kind = "equipment" if model.get_equipment(ref) else ("line" if model.get_line(ref) else None)
            if not kind:
                rejected.append(f"Cannot attach {op.function} to '{op.attached_to}': not an equipment or line tag.")
                continue
            new.added_instruments.append(
                AddedInstrument(function=op.function.upper(), attached_kind=kind, attached_ref=ref, rationale=op.rationale)  # type: ignore[arg-type]
            )
            applied.append(f"Added {op.function.upper()} on {ref}.")

        elif isinstance(op, RemoveItem):
            tag = op.tag.strip().upper()
            found = model.find(tag)
            if not found:
                rejected.append(f"'{op.tag}' is not in the model.")
                continue
            kind, _ = found
            if kind in ("instrument", "inline", "loop"):
                new.suppressed[tag] = op.reason
                applied.append(f"Removed {tag} (recorded as reviewer waiver: {op.reason}).")
            elif kind == "equipment":
                stage = model.get_equipment(tag).stage  # type: ignore[union-attr]
                if stage in optional:
                    new.disabled_stages = sorted(set(new.disabled_stages) | {stage})
                    applied.append(f"Removed optional stage '{stage}' (all trains).")
                else:
                    rejected.append(f"{tag} performs required stage '{stage}'; change the train count instead.")
            else:
                rejected.append(f"{tag} is a {kind}; it follows from the process topology and cannot be removed directly.")

        elif isinstance(op, MoveItem):
            tag = op.tag.strip().upper()
            if not (model.get_equipment(tag) or any(n.tag == tag for n in model.piping_nodes)):
                rejected.append(f"Only equipment and piping nodes can be moved ('{op.tag}').")
                continue
            dx, dy = new.layout_offsets.get(tag, (0.0, 0.0))
            new.layout_offsets[tag] = (dx + op.dx, dy + op.dy)
            applied.append(f"Moved {tag} on the drawing by ({op.dx:+g}, {op.dy:+g}).")

    return new, applied, rejected, answers
