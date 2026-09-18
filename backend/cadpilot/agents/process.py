"""Process Agent — what process is happening, which stages, in what order, under which constraints."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..model.engineering import ProcessDefinition, ProcessStage, Quantity, Stream
from ..model.proposals import ProcessProposal
from .context import AgentContext
from .llm import get_llm

AGENT = "process_agent"


class _LLMProcessPlan(BaseModel):
    """What the LLM is allowed to decide about the process (everything else is rules)."""

    exclude_optional_stages: list[str] = Field(
        description="Ids of OPTIONAL template stages the requirements explicitly say are not wanted"
    )
    constraints: list[str] = Field(description="Process constraints stated or implied by the requirements")
    assumptions: list[str] = Field(description="Assumptions made where the requirements are silent")


def ordered_stage_ids(template: dict, order_override: list[str] | None) -> list[str]:
    ids = [s["id"] for s in template["stages"]]
    if not order_override:
        return ids
    known = [s for s in order_override if s in ids]
    return known + [s for s in ids if s not in known]


def run(ctx: AgentContext) -> ProcessProposal:
    tpl = ctx.template
    intent = ctx.intent
    assumptions: list[str] = []
    constraints = list(intent.constraints)
    excluded: set[str] = set(intent.disabled_stages)
    used_llm = False

    optional = {s["id"]: s for s in tpl["stages"] if s.get("optional")}
    llm = get_llm()
    if llm.enabled and intent.request:
        plan = llm.structured(
            system=(
                "You are the Process Agent of CadPilot, an autonomous P&ID drafting system for dairy plants. "
                "You read the user's requirements and decide only which optional process stages are "
                "explicitly unwanted, and which process constraints apply. Never invent stages."
            ),
            prompt=(
                f"Process template: {tpl['name']}\n"
                f"Stages (id: name, optional?):\n"
                + "\n".join(f"- {s['id']}: {s['name']}{' (optional)' if s.get('optional') else ''}" for s in tpl["stages"])
                + f"\n\nDesign criteria from the client data:\n" + "\n".join(ctx.source.design_criteria[:20])
                + f"\n\nUser requirements:\n{intent.request}"
            ),
            schema=_LLMProcessPlan,
            purpose="process_agent",
            memory=ctx.intent.llm_memory,
        )
        if plan:
            used_llm = True
            excluded |= {s for s in plan.exclude_optional_stages if s in optional}
            constraints += plan.constraints
            assumptions += plan.assumptions

    excluded -= set(intent.enabled_stages)
    excluded &= set(optional)  # only optional stages may be dropped
    stages_by_id = {s["id"]: s for s in tpl["stages"]}
    stages = [
        ProcessStage(
            id=sid,
            name=stages_by_id[sid]["name"],
            kind=stages_by_id[sid]["kind"],
            group=stages_by_id[sid]["group"],
            area=str(stages_by_id[sid]["area"]),
            equipment_type=stages_by_id[sid].get("equipment_type"),
            function=stages_by_id[sid].get("function", ""),
            outlet_service=stages_by_id[sid].get("outlet_service"),
            optional=bool(stages_by_id[sid].get("optional")),
        )
        for sid in ordered_stage_ids(tpl, intent.stage_order)
        if sid not in excluded
    ]
    for sid in sorted(excluded):
        assumptions.append(f"Optional stage '{stages_by_id[sid]['name']}' excluded")

    # Keep terminals at the ends whatever order corrections requested
    stages = [s for s in stages if s.kind == "terminal_in"] + [s for s in stages if s.kind == "equipment"] + [
        s for s in stages if s.kind == "terminal_out"
    ]

    groups = {g: int(v.get("default_count", 1)) for g, v in tpl["groups"].items()}
    basis: dict[str, float | str] = {}
    intake = ctx.source.daily_intake_litres()
    raw = ctx.source.mass_balance_inputs[0] if ctx.source.mass_balance_inputs else None
    if intake:
        basis["daily_intake_l"] = intake
    else:
        assumptions.append("No mass balance intake found; capacity checks against intake skipped")
    if raw and raw.fat is not None:
        basis["raw_milk_fat"] = raw.fat
    if raw and raw.snf is not None:
        basis["raw_milk_snf"] = raw.snf
    if ctx.source.plant_title:
        basis["plant"] = ctx.source.plant_title

    services = ctx.standards.process_templates.get("services", {})
    streams = []
    for code in dict.fromkeys(s.outlet_service for s in stages if s.outlet_service):
        props = {}
        if code == "RM" and raw:
            props = {k: v for k, v in (("fat", raw.fat), ("snf", raw.snf)) if v is not None}
        streams.append(
            Stream(
                id=code,
                name=services.get(code, {}).get("name", code),
                service=code,
                fluid=services.get(code, {}).get("fluid", code),
                design_flow=Quantity(value=0, unit="KLPH"),  # set by merge from line flows
                properties=props,
            )
        )

    return ProcessProposal(
        process=ProcessDefinition(
            template=intent.template,
            name=tpl["name"],
            stages=stages,
            groups=groups,
            basis=basis,
            constraints=constraints,
        ),
        streams=streams,
        assumptions=assumptions,
        used_llm=used_llm,
    )
