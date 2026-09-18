"""Equipment Agent — which equipment, what type, capacity and quantity; tags come from the tag service."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..ingest.excel import EquipmentRow
from ..model.engineering import Equipment, Provenance, Quantity
from ..model.proposals import EquipmentProposal
from .context import AgentContext
from .llm import get_llm

AGENT = "equipment_agent"


class _StageMatch(BaseModel):
    stage_id: str
    row_ref: str | None = Field(description="The `ref` of the design-data row for this stage, or null if none fits")
    reason: str


class _LLMEquipmentMapping(BaseModel):
    matches: list[_StageMatch]


def _keyword_match(stage: dict, rows: list[EquipmentRow]) -> EquipmentRow | None:
    keys = [k.lower() for k in stage.get("match", [])]
    if not keys:
        return None
    return next((r for r in rows if all(k in r.description.lower() for k in keys)), None)


def _llm_match(ctx: AgentContext, stages: list[dict]) -> dict[str, EquipmentRow | None] | None:
    llm = get_llm()
    if not llm.enabled:
        return None
    rows = ctx.source.equipment_rows
    listing = "\n".join(f"{r.ref} | {r.section} | {r.description} | {r.capacity_raw} | qty {r.qty}" for r in rows)
    result = llm.structured(
        system=(
            "You are the Equipment Agent of CadPilot. Map each process stage to the single design-data "
            "row that supplies that equipment for the MAIN milk line. Prefer rows in reception/processing "
            "sections; ignore cream, curd, paneer and CIP equipment unless the stage says so."
        ),
        prompt="Stages:\n"
        + "\n".join(f"- {s['id']}: {s['name']} ({s['equipment_type']})" for s in stages)
        + f"\n\nDesign data rows (ref | section | description | capacity | qty):\n{listing}",
        schema=_LLMEquipmentMapping,
        purpose="equipment_agent",
        memory=ctx.intent.llm_memory,
    )
    if not result:
        return None
    by_ref = {r.ref: r for r in rows}
    return {m.stage_id: by_ref.get(m.row_ref or "") for m in result.matches}


def run(ctx: AgentContext) -> EquipmentProposal:
    tpl = ctx.template
    tagger = ctx.tagger
    stages = [s for s in tpl["stages"] if s["kind"] == "equipment"]
    rows = ctx.source.equipment_rows
    llm_map = _llm_match(ctx, stages)
    assumptions: list[str] = []
    warnings: list[str] = []
    matched: dict[str, EquipmentRow | None] = {}
    for s in stages:
        row = (llm_map or {}).get(s["id"]) or _keyword_match(s, rows)
        matched[s["id"]] = row

    # Train counts: the lead stage's quantity in the design data defines the group size
    group_counts: dict[str, int] = {}
    for g, cfg in tpl["groups"].items():
        lead = matched.get(cfg["lead_stage"])
        if lead and lead.qty:
            group_counts[g] = int(lead.qty)
        else:
            group_counts[g] = int(cfg.get("default_count", 1))
            assumptions.append(f"No design data for '{g}' group size; assumed {group_counts[g]}")
    for g, n in ctx.intent.group_counts.items():
        group_counts[g] = n

    equipment: list[Equipment] = []
    source_rows: dict[str, str] = {}
    for s in stages:
        row = matched[s["id"]]
        count = group_counts[s["group"]]
        if row:
            source_rows[s["id"]] = row.ref
            if row.qty and int(row.qty) != count and s["group"] not in ctx.intent.group_counts:
                warnings.append(
                    f"{s['name']}: design data lists {int(row.qty)} nos. but the {s['group']} group has {count} trains"
                )
        capacity = row.capacity if row and row.capacity else (
            Quantity(**s["default_capacity"]) if s.get("default_capacity") else None
        )
        if not (row and row.capacity) and capacity:
            assumptions.append(f"{s['name']}: capacity {capacity} assumed (not found in design data)")
        for train in range(1, count + 1):
            tag = tagger.equipment(f"{s['id']}#{train}", s["equipment_type"], str(s["area"]))
            equipment.append(
                Equipment(
                    tag=tag,
                    type=s["equipment_type"],
                    name=s["name"],
                    stage=s["id"],
                    area=str(s["area"]),
                    train=train,
                    capacity=capacity,
                    attributes=dict(s.get("attributes", {})),
                    provenance=Provenance(
                        agent=AGENT,
                        source=row.ref if row else "template default",
                        rationale=(f"Matched design data: {row.description}" if row else "No design data row; template default"),
                    ),
                )
            )
    return EquipmentProposal(
        equipment=equipment,
        group_counts=group_counts,
        source_rows=source_rows,
        assumptions=assumptions,
        warnings=warnings,
        used_llm=llm_map is not None,
    )
