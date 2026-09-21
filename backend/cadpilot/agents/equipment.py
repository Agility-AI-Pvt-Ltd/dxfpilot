"""Equipment Agent — which equipment, what type, capacity and quantity; tags come from the tag service."""

from __future__ import annotations

import re

from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from pydantic import BaseModel, Field

from ..ingest.excel import EquipmentRow
from ..model.engineering import Equipment, Provenance, Quantity
from ..model.proposals import EquipmentProposal
from .context import AgentContext
from .llm import get_llm, mark_source

AGENT = "equipment_agent"


class _StageMatch(BaseModel):
    stage_id: str
    row_ref: str | None = Field(description="The `ref` of the design-data row for this stage, or null if none fits")
    reason: str


class _LLMEquipmentMapping(BaseModel):
    matches: list[_StageMatch]


def keyword_matches(stage: dict, rows: list[EquipmentRow]) -> list[EquipmentRow]:
    """Rows whose description contains every `match` keyword of the stage; rows under one of the
    module's own Excel sections (`sections`, e.g. "ghee making") come first."""
    keys = [k.lower() for k in stage.get("match", [])]
    if not keys:
        return []
    hits = [r for r in rows if all(k in r.description.lower() for k in keys)]
    home = [k.lower() for k in stage.get("sections", [])]
    return sorted(hits, key=lambda r: not any(h in (r.section or "").lower() for h in home))


def _keyword_match(stage: dict, rows: list[EquipmentRow]) -> EquipmentRow | None:
    hits = keyword_matches(stage, rows)
    return hits[0] if hits else None


# ---- candidate pre-filter ------------------------------------------------------------------------
# The model only needs rows that could plausibly supply a stage. Shortlisting cuts the prompt from
# every design-data row to a few per stage, while staying on the safe side: ambiguous rows are kept,
# and a stage with no word match falls back to every unit-compatible row (e.g. descriptions in
# another language), then to every row.

PER_STAGE = 8
_FLOW = {"KLPH", "LPH", "m3/h", "kg/h"}
_VOLUME = {"KL", "L"}
_STOP = {"with", "for", "and", "the", "type", "all", "from", "main", "milk", "raw"}


def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]+", text.lower().replace("_", " ")):
        if len(w) < 3 or w in _STOP:
            continue
        for suffix in ("ation", "ing", "ers", "er", "es", "s"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 4:
                w = w[: -len(suffix)]
                break
        out.add(w)
    return out


def _unit_kind(unit: str | None) -> str | None:
    """flow / volume, or 'other' for a real but different quantity (kg, kVA, CFM...); None if unknown."""
    if unit is None:
        return None
    return "flow" if unit in _FLOW else "volume" if unit in _VOLUME else "other"


def candidate_rows(stages: list[dict], rows: list[EquipmentRow]) -> tuple[list[EquipmentRow], dict[str, str]]:
    """Rows worth showing the model, in sheet order, and how each stage's candidates were chosen."""
    row_words = [_words(r.description) for r in rows]
    keep: set[int] = set()
    how: dict[str, str] = {}
    for s in stages:
        vocab = _words(" ".join([s["name"], s.get("equipment_type", ""), *s.get("match", [])]))
        want = _unit_kind((s.get("default_capacity") or {}).get("unit"))

        def compatible(r: EquipmentRow) -> bool:
            have = _unit_kind(r.capacity.unit) if r.capacity else None
            return want is None or have is None or have == want

        keys = [k.lower() for k in s.get("match", [])]
        scored = []
        for i, r in enumerate(rows):
            score = len(vocab & row_words[i]) + (3 if keys and all(k in r.description.lower() for k in keys) else 0)
            if score and compatible(r):
                scored.append((score, -i))
        if scored:
            chosen = [-neg for _, neg in sorted(scored, reverse=True)[:PER_STAGE]]
            how[s["id"]] = f"{len(chosen)} by words"
        else:  # nothing in common (other language, unusual wording): keep every unit-compatible row
            chosen = [i for i, r in enumerate(rows) if r.capacity is not None and compatible(r)]
            how[s["id"]] = f"{len(chosen)} unit-compatible (no word match)"
            if not chosen:
                chosen = list(range(len(rows)))
                how[s["id"]] = "all rows (no word or unit match)"
        keep.update(chosen)
    return [r for i, r in enumerate(rows) if i in keep], how


def _llm_match(ctx: AgentContext, stages: list[dict]) -> dict[str, EquipmentRow | None] | None:
    llm = get_llm()
    if not llm.enabled:
        return None
    all_rows = ctx.source.equipment_rows
    rows, how = candidate_rows(stages, all_rows)
    if (run := get_current_run_tree()) is not None:
        run.metadata.update({"rows_total": len(all_rows), "rows_sent": len(rows), "shortlist": how})
    listing = "\n".join(f"{r.ref} | {r.section} | {r.description} | {r.capacity_raw} | qty {r.qty}" for r in rows)
    result = llm.structured(
        system=(
            "You are the Equipment Agent of CadPilot. Map each process stage to the single design-data "
            "row that supplies that equipment for the MAIN milk line. Prefer rows in reception/processing "
            "sections; ignore cream, curd, paneer and CIP equipment unless the stage says so."
        ),
        prompt="Stages:\n"
        + "\n".join(f"- {s['id']}: {s['name']} ({s['equipment_type']})" for s in stages)
        + f"\n\nCandidate design data rows, shortlisted from {len(all_rows)} "
        + f"(ref | section | description | capacity | qty):\n{listing}",
        schema=_LLMEquipmentMapping,
        purpose="equipment_agent",
        memory=ctx.intent.llm_memory,
    )
    if not result:
        return None
    by_ref = {r.ref: r for r in all_rows}  # the answer is checked against every row, not just the shortlist
    return {m.stage_id: by_ref.get(m.row_ref or "") for m in result.matches}


@traceable(name="equipment_agent", run_type="chain", process_inputs=lambda d: d["ctx"].trace_summary())
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
        if cfg.get("train_labels") or cfg.get("fixed") or not cfg.get("lead_stage"):
            # named trains (e.g. lye / acid / water tanks) are a design decision, not a purchase quantity
            group_counts[g] = int(cfg.get("default_count", len(cfg.get("train_labels", [])) or 1))
            continue
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
        labels = tpl["groups"].get(s["group"], {}).get("train_labels", [])
        for train in range(1, count + 1):
            tag = tagger.equipment(f"{s['id']}#{train}", s["equipment_type"], str(s["area"]))
            equipment.append(
                Equipment(
                    tag=tag,
                    type=s["equipment_type"],
                    name=f"{s['name']} — {labels[train - 1]}" if train <= len(labels) else s["name"],
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
    mark_source(
        "llm" if llm_map is not None else ("rules (fallback: model reply unusable)" if get_llm().enabled else "rules"),
        rows_matched_by="llm" if llm_map is not None else "keywords",
    )
    return EquipmentProposal(
        equipment=equipment,
        group_counts=group_counts,
        source_rows=source_rows,
        assumptions=assumptions,
        warnings=warnings,
        used_llm=llm_map is not None,
    )
