"""Design basis — the numbers the engineering solver is allowed to use, and where each comes from.

The workbooks often disagree: the design criteria say 10 LLPD, the mass balance 5 LLPD; the design
criteria plan 2 × 30 KLPH processing lines, the cost estimate buys 2 × 20 KLPH pasteurizers. The
system never silently picks one:

    extract every candidate value (with file, sheet, row and the original text)
        → agreed / single source   → used as the basis
        → CONFLICT                  → a human chooses (or enters) the value → approved basis
                                    → the solver runs on the approved basis

Every parameter says what it drives (`effect`), so an approval visibly changes the drawing.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field

TOLERANCE = 0.05  # values within 5 % agree
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


class Candidate(BaseModel):
    value: float
    display: str  # "10 LLPD = 1,000,000 L/day"
    source: str  # "Design criteria" | "Mass balance" | "Cost estimate"
    ref: str | None = None  # "file:sheet!R5"
    text: str = ""  # the original cell text


class Approval(BaseModel):
    value: float
    source: str  # which candidate (or "entered by reviewer")
    ref: str | None = None
    note: str = ""
    by: str = "reviewer"
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


class Parameter(BaseModel):
    id: str
    name: str
    unit: str
    effect: str  # what the solver does with it
    modules: list[str]  # sections that depend on it ("*" = all)
    candidates: list[Candidate]
    status: str  # agreed | single_source | conflict | approved | missing
    value: float | None = None  # what the solver uses (None: no basis)
    approval: Approval | None = None

    @property
    def needs_decision(self) -> bool:
        return self.status == "conflict"


class DesignBasis(BaseModel):
    parameters: list[Parameter]

    def get(self, pid: str) -> Parameter | None:
        return next((p for p in self.parameters if p.id == pid), None)

    def conflicts(self, modules: list[str] | None = None) -> list[Parameter]:
        return [p for p in self.parameters if p.needs_decision and _relevant(p, modules)]

    def values(self) -> dict[str, float]:
        return {p.id: p.value for p in self.parameters if p.value is not None}


def _relevant(p: Parameter, modules: list[str] | None) -> bool:
    return modules is None or "*" in p.modules or bool(set(p.modules) & set(modules))


def _num(s: str) -> float | None:
    s = s.strip().lower()
    if s in _WORDS:
        return float(_WORDS[s])
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _criteria(source) -> list[tuple[str, str | None]]:
    refs = list(getattr(source, "design_criteria_refs", []) or [])
    return [(line, refs[i] if i < len(refs) else None) for i, line in enumerate(source.design_criteria)]


def _find(source, pattern: str) -> list[tuple[re.Match, str, str | None]]:
    return [(m, line, ref) for line, ref in _criteria(source) for m in [re.search(pattern, line, re.I)] if m]


def _rows(source, *keys: str):
    return [r for r in source.equipment_rows if all(k in r.description.lower() for k in keys)]


def _products(source, *keys: str) -> tuple[float, list]:
    picked = [o for o in source.mass_balance_outputs if any(k in o.name.lower() for k in keys)]
    return sum((o.litres or o.kg or 0) for o in picked), picked


def _decide(p: Parameter, approvals: dict) -> Parameter:
    vals = [c.value for c in p.candidates]
    if not vals:
        p.status, p.value = "missing", None
    elif all(abs(v - vals[0]) <= TOLERANCE * max(abs(vals[0]), 1e-9) for v in vals):
        p.status, p.value = ("agreed" if len(vals) > 1 else "single_source"), vals[0]
    else:
        p.status, p.value = "conflict", None
    ap = approvals.get(p.id)
    if ap:
        p.approval = Approval(**ap) if isinstance(ap, dict) else ap
        p.status, p.value = "approved", p.approval.value
    return p


def extract(source, approvals: dict | None = None) -> DesignBasis:
    """Every design parameter the solver uses, with all the values the workbooks give for it."""
    approvals = approvals or {}
    params: list[Parameter] = []

    # ---- plant capacity (milk intake per day) ------------------------------------------------------------
    cands: list[Candidate] = []
    intake = source.daily_intake_litres() if source.mass_balance_inputs else None
    if intake:
        first = source.mass_balance_inputs[0]
        cands.append(Candidate(value=intake, display=f"{intake:,.0f} L/day ({intake / 1e5:g} LLPD)", source="Mass balance",
                               ref=first.ref, text=f"{first.name}: {first.litres:,.0f} L"))
    for m, line, ref in _find(source, r"(reception|processing)[^|]*\|\s*([\d.]+)\s*LLPD"):
        v = float(m.group(2)) * 1e5
        cands.append(Candidate(value=v, display=f"{m.group(2)} LLPD = {v:,.0f} L/day", source="Design criteria", ref=ref, text=line))
    title = getattr(source, "plant_title", None) or ""
    tm = re.search(r"capacity\s*([\d.]+)\s*LLPD(?:\s*expandable\s*to\s*([\d.]+)\s*LLPD)?", title, re.I)
    if tm:
        v = float(tm.group(1)) * 1e5
        note = f" (expandable to {tm.group(2)} LLPD)" if tm.group(2) else ""
        cands.append(Candidate(value=v, display=f"{tm.group(1)} LLPD = {v:,.0f} L/day{note}", source="Design criteria (title)",
                               ref=getattr(source, "plant_title_ref", None), text=title))
    params.append(Parameter(id="plant_capacity", name="Plant capacity (milk intake)", unit="L/day", candidates=cands, modules=["*"],
                            status="", effect="the milk intake the reception, storage, pasteurization and separation are sized on"))

    # ---- processing line capacity -------------------------------------------------------------------------
    cands = []
    for m, line, ref in _find(source, r"(\w+)\s+milk processing lines?\s+each of\s+([\d.]+)\s*KLPH"):
        n = _num(m.group(1))
        cands.append(Candidate(value=float(m.group(2)), display=f"{m.group(2)} KLPH per line" + (f" ({n:g} lines)" if n else ""),
                               source="Design criteria", ref=ref, text=line))
    for r in _rows(source, "milk pasteurizer")[:1]:
        if r.capacity and r.capacity.unit == "KLPH":
            cands.append(Candidate(value=r.capacity.value, display=f"{r.capacity} per pasteurizer" + (f" (× {r.qty:g})" if r.qty else ""),
                                   source="Cost estimate", ref=r.ref, text=r.description.splitlines()[0]))
    params.append(Parameter(id="processing_line_klph", name="Processing line capacity (per pasteurizer)", unit="KLPH",
                            candidates=cands, modules=["pasteurization", "classic"], status="",
                            effect="the unit capacity of each pasteurizer: pasteurizers = ⌈required flow ÷ this⌉"))

    # ---- liquid milk packing machines ---------------------------------------------------------------------
    cands = []
    for m, line, ref in _find(source, r"(\w+)\s+packing lines?\s+each with\s+(\d+)"):
        lines_, per = _num(m.group(1)), _num(m.group(2))
        if lines_ and per:
            cands.append(Candidate(value=lines_ * per, display=f"{lines_:g} lines × {per:g} machines = {lines_ * per:g}",
                                   source="Design criteria", ref=ref, text=line))
    for r in _rows(source, "pouch filling machines", "liquid milk")[:1]:
        if r.qty:
            cands.append(Candidate(value=r.qty, display=f"{r.qty:g} machines ({r.capacity_raw})", source="Cost estimate",
                                   ref=r.ref, text=r.description.splitlines()[0]))
    params.append(Parameter(id="packing_machines", name="Liquid milk pouch filling machines", unit="machines",
                            candidates=cands, modules=["packaging"], status="",
                            effect="the number of liquid-milk pouch fillers drawn (fixes the count)"))

    # ---- product capacities (design criteria vs mass balance) ---------------------------------------------
    for pid, name, pattern, keys, module, scale in (
        ("liquid_milk_lpd", "Liquid milk packed", r"liquid milk packing\s*\|\s*([\d.]+)\s*TLPD", ("liquid milk",), "packaging", 1000),
        ("curd_kgpd", "Curd production", r"curd production\s*\|\s*([\d.]+)\s*MTPD", ("curd",), "curd", 1000),
        ("ghee_kgpd", "Ghee production", r"ghee production\s*\|\s*([\d.]+)\s*MTPD", ("ghee",), "ghee", 1000),
    ):
        cands = []
        total, picked = _products(source, *keys)
        if total:
            cands.append(Candidate(value=total, display=f"{total:,.0f} {'L' if scale == 1000 and pid.endswith('lpd') else 'kg'}/day "
                                   f"({' + '.join(o.name for o in picked)})", source="Mass balance",
                                   ref=picked[0].ref, text="; ".join(f"{o.name} {o.litres or o.kg:,.0f}" for o in picked)))
        for m, line, ref in _find(source, pattern):
            v = float(m.group(1)) * scale
            unit = "TLPD" if "TLPD" in pattern else "MTPD"
            cands.append(Candidate(value=v, display=f"{m.group(1)} {unit} = {v:,.0f} {'L' if unit == 'TLPD' else 'kg'}/day",
                                   source="Design criteria", ref=ref, text=line))
        params.append(Parameter(id=pid, name=name, unit="L/day" if pid.endswith("lpd") else "kg/day", candidates=cands,
                                modules=[module], status="",
                                effect=f"the daily {name.lower()} the {module} section is sized on"))

    return DesignBasis(parameters=[_decide(p, approvals) for p in params])
