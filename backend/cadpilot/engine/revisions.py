"""Revision diffing: what changed between two committed engineering models (by tag identity)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..model.engineering import EngineeringModel


class CategoryDiff(BaseModel):
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    modified: dict[str, list[str]] = Field(default_factory=dict)  # tag -> changed fields
    unchanged: int = 0


class ModelDiff(BaseModel):
    equipment: CategoryDiff = Field(default_factory=CategoryDiff)
    lines: CategoryDiff = Field(default_factory=CategoryDiff)
    instruments: CategoryDiff = Field(default_factory=CategoryDiff)
    valves: CategoryDiff = Field(default_factory=CategoryDiff)
    piping_nodes: CategoryDiff = Field(default_factory=CategoryDiff)

    def changed_tags(self) -> set[str]:
        out: set[str] = set()
        for cat in (self.equipment, self.lines, self.instruments, self.valves, self.piping_nodes):
            out |= set(cat.added) | set(cat.modified)
        return out

    def is_empty(self) -> bool:
        return not self.changed_tags() and not any(
            c.removed for c in (self.equipment, self.lines, self.instruments, self.valves, self.piping_nodes)
        )

    def summary(self) -> str:
        parts = []
        for name, cat in (
            ("equipment", self.equipment),
            ("connections", self.lines),
            ("instruments", self.instruments),
            ("valves", self.valves),
        ):
            n = len(cat.added) + len(cat.removed) + len(cat.modified)
            if n:
                bits = []
                if cat.added:
                    bits.append(f"{len(cat.added)} added")
                if cat.removed:
                    bits.append(f"{len(cat.removed)} removed")
                if cat.modified:
                    bits.append(f"{len(cat.modified)} modified")
                parts.append(f"{name}: {', '.join(bits)}")
        if not parts:
            return "No engineering changes."
        kept = self.equipment.unchanged + self.lines.unchanged + self.instruments.unchanged
        return "; ".join(parts) + f". {kept} other elements preserved."


def _compare(old: dict[str, dict], new: dict[str, dict]) -> CategoryDiff:
    d = CategoryDiff()
    d.added = sorted(set(new) - set(old))
    d.removed = sorted(set(old) - set(new))
    for tag in sorted(set(old) & set(new)):
        fields = [k for k in new[tag] if old[tag].get(k) != new[tag].get(k)]
        if fields:
            d.modified[tag] = fields
        else:
            d.unchanged += 1
    return d


def diff_models(old: EngineeringModel, new: EngineeringModel) -> ModelDiff:
    def eq(m: EngineeringModel) -> dict[str, dict]:
        return {
            e.tag: {"type": e.type, "name": e.name, "stage": e.stage, "capacity": str(e.capacity), "attributes": e.attributes}
            for e in m.equipment
        }

    def lines(m: EngineeringModel) -> dict[str, dict]:
        return {
            ln.tag: {
                "from": str(ln.from_),
                "to": str(ln.to),
                "service": ln.service,
                "size_dn": ln.size_dn,
                "design_flow": str(ln.design_flow),
                "inline": [c.tag for c in ln.inline],
            }
            for ln in m.lines
        }

    def inst(m: EngineeringModel) -> dict[str, dict]:
        return {
            i.tag: {"function": i.function, "attached_to": i.attached_to.ref, "loop": i.loop, "alarms": i.alarms}
            for i in m.instruments
        }

    def valves(m: EngineeringModel) -> dict[str, dict]:
        return {c.tag: {"type": c.type, "line": ln.tag} for ln in m.lines for c in ln.inline}

    def nodes(m: EngineeringModel) -> dict[str, dict]:
        return {n.tag: {"kind": n.kind} for n in m.piping_nodes}

    return ModelDiff(
        equipment=_compare(eq(old), eq(new)),
        lines=_compare(lines(old), lines(new)),
        instruments=_compare(inst(old), inst(new)),
        valves=_compare(valves(old), valves(new)),
        piping_nodes=_compare(nodes(old), nodes(new)),
    )


def next_revision(letter: str | None) -> str:
    if not letter:
        return "A"
    chars = list(letter)
    i = len(chars) - 1
    while i >= 0:
        if chars[i] != "Z":
            chars[i] = chr(ord(chars[i]) + 1)
            return "".join(chars)
        chars[i] = "A"
        i -= 1
    return "A" + "".join(chars)
