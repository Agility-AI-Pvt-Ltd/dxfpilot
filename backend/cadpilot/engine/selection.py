"""Mass-balance solver + equipment selection engine — how many units each stage needs.

The LLM may say *which* design-data row supplies a stage (the equipment category). It never decides
how many units or how big: that is arithmetic, done here from the mass balance and the equipment
catalogue (the matched design-data row, or the template's catalogue default):

    required = daily quantity × factor ÷ operating hours           (flow stages:   KLPH)
    required = daily quantity × factor × storage days               (storage stages: KL)
    required = daily quantity × factor ÷ (hours ÷ batch cycle)      (batch stages:   KL per batch)
    units    = ceil(required ÷ unit capacity) + standby

A module (or template) declares the basis of each sized stage under `sizing:`:
    - stage: pz_pasteurizer
      basis: intake                 # the mass-balance milk intake …
      hours: 20                     # … processed in 20 h a day
    - stage: pn_vat
      basis: products               # the mass-balance products named here …
      products: [paneer]
      factor: 5                     # … × 5 L milk per kg of paneer
      hours: 8
      batch_cycle_h: 2              # a batch every 2 h
Stages without a basis keep the design-data quantity, and the reason is recorded.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

from ..model.engineering import Quantity
from .sizing import flow_m3h

_VOLUME_TO_KL = {"KL": 1.0, "L": 0.001, "m3": 1.0}
_MASS_TO_T = {"t": 1.0, "kg": 0.001}


class SizingRecord(BaseModel):
    stage: str
    stage_name: str
    basis: str  # human-readable: "500,000 L/day intake ÷ 20 h"
    required: str  # "25 KLPH"
    unit_capacity: str  # "20 KLPH (R20 Milk Pasteurizer …)"
    ratio: float  # required ÷ unit capacity
    working: int
    standby: int
    units: int  # working + standby — what is drawn
    design_data_qty: int | None = None  # what the cost estimate lists, for comparison
    source: str  # design-data row or "catalogue default"
    source_ref: str | None = None  # "file:sheet!R20" of the unit capacity
    basis_used: list[dict] = []  # design-basis parameters used: {id, name, value, status, source, ref}
    note: str = ""


# design-basis parameters that replace a group of mass-balance products when approved / agreed
PRODUCT_BASIS = {"liquid milk": "liquid_milk_lpd", "curd": "curd_kgpd", "ghee": "ghee_kgpd"}


def _used(p) -> dict:
    src = p.approval.source if p.approval else (p.candidates[0].source if p.candidates else "")
    ref = p.approval.ref if p.approval else (p.candidates[0].ref if p.candidates else None)
    return {"id": p.id, "name": p.name, "value": p.value, "unit": p.unit, "status": p.status, "source": src, "ref": ref}


def _daily_litres(entry: dict, source, basis=None, used: list | None = None) -> tuple[float | None, str]:
    """The daily quantity a stage is sized on, and how it was found (the design basis first)."""
    used = used if used is not None else []
    if entry["basis"] == "intake":
        p = basis.get("plant_capacity") if basis else None
        if p is not None and p.value:
            used.append(_used(p))
            tag = "approved" if p.status == "approved" else "agreed"
            return p.value, f"{p.value:,.0f} L/day plant capacity ({tag} design basis)"
        litres = source.daily_intake_litres()
        return (litres, f"{litres:,.0f} L/day milk intake") if litres else (None, "no milk intake in the mass balance")
    keys = [k.lower() for k in entry.get("products", [])]
    picked = [o for o in source.mass_balance_outputs if any(k in o.name.lower() for k in keys)]
    if not picked:
        return None, f"no mass-balance product matching {', '.join(keys)}"
    total = sum((o.litres if o.litres else (o.kg or 0)) for o in picked)  # kg ≈ L for dairy products
    parts = []
    for word, pid in PRODUCT_BASIS.items():  # an approved product capacity replaces the mass-balance figure
        p = basis.get(pid) if basis else None
        group = [o for o in picked if word in o.name.lower()]
        if p is not None and p.value and group and p.status in ("approved", "agreed"):
            total += p.value - sum((o.litres or o.kg or 0) for o in group)
            used.append(_used(p))
            parts.append(f"{p.name.lower()} {p.value:,.0f} ({p.status} basis)")
            picked = [o for o in picked if o not in group]
    names = " + ".join([o.name.split("(")[0].strip() for o in picked] + parts)
    return total, f"{total:,.0f} kg/day {names}"


def _unit_capacity(cap: Quantity | None, kind: str) -> float | None:
    """The unit's capacity on the same basis as the requirement (KLPH, KL or t/day)."""
    if cap is None:
        return None
    if kind == "flow":
        return flow_m3h(cap)
    if kind in ("volume", "batch"):
        f = _VOLUME_TO_KL.get(cap.unit)
        return cap.value * f if f else None
    if kind == "pieces":
        return cap.value
    if kind == "mass_per_day":
        if cap.unit in ("t/day", "MTPD"):
            return cap.value
        return None
    return None


def size_stage(entry: dict, stage: dict, capacity: Quantity | None, row, source, design_basis=None) -> SizingRecord | None:
    used: list[dict] = []
    daily, basis = _daily_litres(entry, source, design_basis, used)
    # the reviewer-approved line capacity is the unit capacity of the pasteurizers
    line = design_basis.get("processing_line_klph") if design_basis else None
    if line is not None and line.value and entry.get("unit_basis") == "processing_line_klph":
        capacity = Quantity(value=line.value, unit="KLPH")
        used.append(_used(line))
    factor = float(entry.get("factor", 1.0))
    standby = int(entry.get("standby", 0))
    kind = ("batch" if entry.get("batch_cycle_h") else "volume" if entry.get("storage_days") is not None
            else "mass_per_day" if entry.get("unit") == "t/day"
            else "pieces" if capacity is not None and capacity.unit in ("CPH", "PPH") else "flow")
    src = f"{row.ref.split('!')[-1]} {row.description.splitlines()[0][:40]}" if row else "catalogue default"
    src_ref = row.ref if row else None
    qty = int(row.qty) if row and row.qty else None
    if not daily:
        return SizingRecord(stage=stage["id"], stage_name=stage["name"], basis=basis, required="—",
                            unit_capacity=str(capacity) if capacity else "—", ratio=0, working=0, standby=0, units=0,
                            design_data_qty=qty, source=src, source_ref=src_ref, basis_used=used, note="no mass-balance basis: the design-data quantity is kept")
    unit = _unit_capacity(capacity, kind)
    fac = f" × {factor:g}" if factor != 1 else ""
    if kind == "flow":
        hours = float(entry.get("hours", 20))
        need, need_s, how = daily * factor / hours / 1000, "KLPH", f"{basis}{fac} ÷ {hours:g} h"
    elif kind == "volume":
        days = float(entry["storage_days"])
        need, need_s, how = daily * factor * days / 1000, "KL", f"{basis}{fac} × {days:g} day storage"
    elif kind == "batch":
        hours, cycle = float(entry.get("hours", 8)), float(entry["batch_cycle_h"])
        batches = max(hours / cycle, 1)
        need, need_s, how = daily * factor / batches / 1000, "KL per batch", f"{basis}{fac} in {batches:g} batches ({hours:g} h ÷ {cycle:g} h cycle)"
    elif kind == "pieces":  # filling machines rated in cups / pouches per hour
        hours = float(entry.get("hours", 8))
        need, need_s, how = daily * factor / hours, capacity.unit, f"{basis}{fac} (packs) ÷ {hours:g} h"
    else:  # mass per day (e.g. a dryer rated in t/day)
        need, need_s, how = daily * factor / 1000, "t/day", f"{basis}{fac}"
    if not unit:
        return SizingRecord(stage=stage["id"], stage_name=stage["name"], basis=how, required=f"{need:,.2f} {need_s}",
                            unit_capacity=str(capacity) if capacity else "—", ratio=0, working=0, standby=0, units=0,
                            design_data_qty=qty, source=src, source_ref=src_ref, basis_used=used, note="unit capacity not comparable: the design-data quantity is kept")
    ratio = need / unit
    working = max(1, math.ceil(ratio - 1e-9))
    note = ""
    if entry.get("per_product"):  # one machine per product (no changeovers), at least
        n_products = len([o for o in source.mass_balance_outputs if any(k.lower() in o.name.lower() for k in entry.get("products", []))])
        if n_products > working:
            working, note = n_products, f"one per product ({n_products} products)"
    return SizingRecord(
        stage=stage["id"], stage_name=stage["name"], basis=how, required=f"{need:,.2f} {need_s}",
        unit_capacity=f"{capacity} per unit", ratio=round(ratio, 2), working=working, standby=standby,
        units=working + standby, design_data_qty=qty, source=src, source_ref=src_ref, basis_used=used, note=note,
    )


def select(tpl: dict, stages: list[dict], matched: dict, capacities: dict, source, design_basis=None) -> tuple[dict[str, int], list[SizingRecord]]:
    """Group sizes from the mass balance. Returns (group → units, the calculation per sized stage)."""
    by_id = {s["id"]: s for s in stages}
    counts: dict[str, int] = {}
    records: list[SizingRecord] = []
    for entry in tpl.get("sizing", []):
        st = by_id.get(entry["stage"])
        if st is None:
            continue
        rec = size_stage(entry, st, capacities.get(st["id"]), matched.get(st["id"]), source, design_basis)
        if rec is None:
            continue
        fixed = design_basis.get(entry["count_basis"]) if design_basis and entry.get("count_basis") else None
        if fixed is not None and fixed.value:  # an approved count (e.g. 4 lines × 4 machines) fixes the number
            rec.working, rec.standby, rec.units = int(fixed.value), 0, int(fixed.value)
            rec.basis_used.append(_used(fixed))
            rec.note = f"count fixed by the {fixed.status} design basis ({fixed.name})"
        records.append(rec)
        if rec.units:
            counts[st["group"]] = max(counts.get(st["group"], 0), rec.units)
    return counts, records
