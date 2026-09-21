"""Scope agent — which plant sections (engineering modules) do the workbooks describe?

Evidence first: every module looks for its own equipment in the design data (the same keyword
matching the equipment agent uses, preferring rows under the module's own Excel sections). A
module is *found* when at least two of its equipment items, or half of them, have a row; *partly
found* when some evidence exists but not enough. The LLM then reviews the section headings and may
add a module the keywords missed (another language, other wording) — but only by citing rows that
exist; it cannot remove a module the rows prove. The reviewer sees the evidence and edits the choice.
"""

from __future__ import annotations

import re

from langsmith import traceable
from pydantic import BaseModel, Field

from ..ingest.excel import SourceData
from ..standards import Standards
from .equipment import keyword_matches, train_rows
from .llm import get_llm, mark_source

AGENT = "scope_agent"


class Evidence(BaseModel):
    ref: str  # row reference, e.g. "Eqpt. Cost Est.!R12"
    text: str  # the row's description as written in the workbook
    capacity: str | None = None
    qty: float | None = None
    stage: str | None = None  # the module stage it supplies (None when cited by the AI)


class ModuleFinding(BaseModel):
    id: str
    name: str
    status: str  # found | partial | not_found
    found_by: str  # rows | ai | none
    matched: int  # equipment items of the module that have a row
    total: int
    evidence: list[Evidence] = Field(default_factory=list)
    note: str = ""


class ScopeDetection(BaseModel):
    modules: list[ModuleFinding]
    selected: list[str]  # the modules to draw (found ones)
    used_llm: bool = False

    def summary(self) -> str:
        found = [m for m in self.modules if m.status == "found"]
        partial = [m for m in self.modules if m.status == "partial"]
        lines = [f"I read your workbooks and found {len(found)} plant section{'s' if len(found) != 1 else ''}:"]
        for m in found:
            rows = ", ".join(f"{e.ref.split('!')[-1]} {_short(e.text)}" for e in m.evidence[:3])
            more = f" (+{len(m.evidence) - 3} more)" if len(m.evidence) > 3 else ""
            by = " — identified by the AI" if m.found_by == "ai" else ""
            lines.append(f"• {m.name} — {rows}{more}{by}")
        if partial:
            lines.append("Only partly in the workbooks, not drawn: " + "; ".join(
                f"{m.name} ({', '.join(_short(e.text) for e in m.evidence[:2])})" for m in partial))
        lines.append("Drawing them now. To add or remove a section, open Design data → Plant sections and regenerate.")
        return "\n".join(lines)


def _short(text: str, n: int = 44) -> str:
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


class _AIModule(BaseModel):
    module_id: str
    row_refs: list[str] = Field(description="Refs of design-data rows (exactly as listed) that belong to this plant section")
    reason: str


class _AIScope(BaseModel):
    present: list[_AIModule] = Field(description="Plant sections the equipment list clearly contains; empty if none")


def _rule_findings(source: SourceData, standards: Standards) -> list[ModuleFinding]:
    rows = source.equipment_rows
    out = []
    for mid, m in standards.modules.items():
        stages = [s for s in standards.template(mid)["stages"] if s["kind"] == "equipment"]
        evidence: list[Evidence] = []
        used: set[str] = set()
        package_stages: set[str] = set()
        for s in stages:
            hits = [h for h in train_rows(s, rows) if h] if s.get("train_match") else \
                [next((r for r in keyword_matches(s, rows) if r.ref not in used), None)]
            if not any(hits) and s.get("package"):  # supplied as part of a lump-sum package row
                pkg = keyword_matches({**s, "match": s["package"]}, rows)
                if pkg:
                    package_stages.add(s["name"])
                    hits = [pkg[0]]
            for hit in hits:
                if hit is None or hit.ref in used:
                    continue
                used.add(hit.ref)
                evidence.append(Evidence(ref=hit.ref, text=hit.description.splitlines()[0].strip(), capacity=hit.capacity_raw,
                                         qty=hit.qty, stage=s["name"]))
        n, total = len({e.stage for e in evidence} | package_stages), len(stages)
        status = "found" if n >= min(2, total) or (total and n / total >= 0.5) else ("partial" if n else "not_found")
        out.append(ModuleFinding(id=mid, name=m["name"], status=status, found_by="rows" if n else "none",
                                 matched=n, total=total, evidence=evidence))
    return out


def _ai_review(source: SourceData, standards: Standards, findings: list[ModuleFinding], memory: dict | None) -> list[_AIModule] | None:
    llm = get_llm()
    if not llm.enabled or not source.equipment_rows:
        return None
    by_section: dict[str, list[str]] = {}
    for r in source.equipment_rows:
        by_section.setdefault(r.section or "(no section)", []).append(f"{r.ref} | {r.description.splitlines()[0][:70]}")
    listing = "\n".join(f"## {sec}\n" + "\n".join(rows[:25]) for sec, rows in by_section.items())
    catalogue = "\n".join(
        f"- {mid}: {m['name']} — {m.get('summary', '')}; equipment: "
        + ", ".join(s["name"] for s in m["stages"] if s["kind"] == "equipment")
        for mid, m in standards.modules.items()
    )
    known = ", ".join(f"{f.id}={f.status}" for f in findings)
    result = llm.structured(
        system=(
            "You identify which plant sections a dairy plant's equipment list (a cost estimate) contains. "
            "A section is present only if the list has equipment that clearly belongs to it — cite those rows by "
            "their exact ref. Do not guess from the plant type; a section with no cited row is not present."
        ),
        prompt=f"Plant sections (engineering modules):\n{catalogue}\n\nWhat keyword matching already found: {known}\n\n"
               f"Equipment list by section heading:\n{listing}\n\nWhich sections are present?",
        schema=_AIScope,
        purpose="scope_agent",
        memory=memory,
    )
    return result.present if result else None


@traceable(name="scope_agent", run_type="chain", process_inputs=lambda d: {"rows": len(d["source"].equipment_rows)})
def detect(source: SourceData, standards: Standards, memory: dict | None = None, use_llm: bool = True) -> ScopeDetection:
    findings = _rule_findings(source, standards)
    ai = _ai_review(source, standards, findings, memory) if use_llm else None
    by_ref = {r.ref: r for r in source.equipment_rows}
    by_id = {f.id: f for f in findings}
    for pick in ai or []:
        f = by_id.get(pick.module_id)
        refs = [ref for ref in pick.row_refs if ref in by_ref]  # the AI may only cite rows that exist
        if f is None or not refs or f.status == "found":
            continue
        have = {e.ref for e in f.evidence}
        f.evidence += [Evidence(ref=ref, text=by_ref[ref].description.splitlines()[0].strip(),
                                capacity=by_ref[ref].capacity_raw, qty=by_ref[ref].qty) for ref in refs if ref not in have]
        f.status, f.found_by, f.note = "found", "ai", pick.reason
    mark_source("llm" if ai is not None else "rules", found=[f.id for f in findings if f.status == "found"])
    return ScopeDetection(modules=findings, selected=[f.id for f in findings if f.status == "found"], used_llm=ai is not None)


# ---- coverage: which rows and products of the workbooks are NOT on the drawing -------------------------
# Every equipment row of a process section is either drawn, or listed here with its row number — a
# drawing that leaves out part of the plant must say so, never do it silently.

PROCESS_UNITS = {"KLPH", "LPH", "m3/h", "kg/h", "KL", "L", "t/h", "t", "kg", "PPH", "CPH", "t/day"}


class Coverage(BaseModel):
    not_drawn: dict[str, list[Evidence]] = Field(default_factory=dict)  # section of a drawn module → rows left out
    uncovered: dict[str, list[Evidence]] = Field(default_factory=dict)  # process section no module covers → its rows
    products_uncovered: list[str] = Field(default_factory=list)  # mass-balance products no module in the library makes
    products_deselected: list[str] = Field(default_factory=list)  # made by a module that is not on this drawing
    rows_total: int = 0
    rows_drawn: int = 0


def _ev(r) -> Evidence:
    return Evidence(ref=r.ref, text=r.description.splitlines()[0].strip(), capacity=r.capacity_raw, qty=r.qty)


def coverage(source: SourceData, standards: Standards, selected: list[str], drawn_refs: set[str]) -> Coverage:
    sel_keys = {k.lower() for m in selected for k in standards.modules[m].get("sections", [])}
    all_keys = {k.lower() for m in standards.modules.values() for k in m.get("sections", [])}
    by_section: dict[str, list] = {}
    for r in source.equipment_rows:
        by_section.setdefault(r.section or "", []).append(r)
    not_pid = [k.lower() for k in standards.validation.get("data", {}).get("not_pid_items", [])]
    by_section = {sec: [r for r in rows if not any(k in r.description.lower() for k in not_pid)] for sec, rows in by_section.items()}
    cov = Coverage()
    for section, rows in by_section.items():
        low = section.lower()
        process = sum(1 for r in rows if r.capacity and r.capacity.unit in PROCESS_UNITS) >= 2
        if any(k in low for k in sel_keys):
            cov.rows_total += len(rows)
            left = [r for r in rows if r.ref not in drawn_refs]
            cov.rows_drawn += len(rows) - len(left)
            if left:
                cov.not_drawn[section] = [_ev(r) for r in left]
        elif process and not any(k in low for k in all_keys):
            cov.rows_total += len(rows)
            cov.uncovered[section] = [_ev(r) for r in rows]
    def makes(mods, name: str) -> list[str]:
        return [m for m in mods if any(re.search(rf"(?<![a-z-]){re.escape(k.lower())}", name) for k in standards.modules[m].get("products", []))]

    for out in source.mass_balance_outputs:
        name = out.name.lower()
        if makes(selected, name):
            continue
        qty = f"{out.kg:,.0f} kg" if out.kg else (f"{out.litres:,.0f} L" if out.litres else "")
        label = f"{out.name} ({qty})" if qty else out.name
        others = makes(list(standards.modules), name)
        if others:
            cov.products_deselected.append(f"{label} — {standards.modules[others[0]]['name']} section not selected")
        else:
            cov.products_uncovered.append(label)
    return cov
