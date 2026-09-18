"""Read mass-balance and equipment/design-criteria workbooks into SourceData.

Parsing locates tables by their header cells rather than by fixed coordinates, so workbooks that
follow the same template but shift rows/columns still load.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import openpyxl
from pydantic import BaseModel, Field

from ..model.engineering import Quantity


class MassBalanceEntry(BaseModel):
    name: str
    litres: float | None = None
    kg: float | None = None
    fat: float | None = None
    snf: float | None = None


class EquipmentRow(BaseModel):
    section: str
    description: str
    capacity_raw: str | None = None
    capacity: Quantity | None = None
    qty: float | None = None
    uom: str | None = None
    ref: str  # "<file>:<sheet>!R<row>" — provenance for every derived object


class SourceData(BaseModel):
    files: list[str] = Field(default_factory=list)
    mass_balance_inputs: list[MassBalanceEntry] = Field(default_factory=list)
    mass_balance_outputs: list[MassBalanceEntry] = Field(default_factory=list)
    design_criteria: list[str] = Field(default_factory=list)
    equipment_rows: list[EquipmentRow] = Field(default_factory=list)
    plant_title: str = ""
    warnings: list[str] = Field(default_factory=list)

    def daily_intake_litres(self) -> float | None:
        total = sum(e.litres or 0 for e in self.mass_balance_inputs)
        return total or None


_UNIT_ALIASES = {
    "klph": "KLPH",
    "kl": "KL",
    "ltr": "L",
    "ltr.": "L",
    "l": "L",
    "lph": "LPH",
    "kg/hr": "kg/h",
    "kg./hr": "kg/h",
    "kg./hr.": "kg/h",
    "mt": "t",
    "cph": "CPH",
    "pph": "PPH",
}


def parse_quantity(raw: object) -> Quantity | None:
    if raw is None:
        return None
    text = str(raw).strip()
    m = re.match(r"^\s*([\d.]+)\s*(?:/\s*[\d.]+\s*)?([A-Za-z./ ]+?)\s*$", text)
    if not m:
        return None
    unit_raw = m.group(2).replace(" ", "").lower().rstrip(".")
    unit = _UNIT_ALIASES.get(unit_raw) or _UNIT_ALIASES.get(unit_raw + ".") or m.group(2).strip()
    try:
        return Quantity(value=float(m.group(1)), unit=unit)
    except ValueError:
        return None


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def _norm(v: object) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def _read_mass_balance(ws, fname: str, out: SourceData) -> bool:
    rows = list(ws.iter_rows(values_only=True))
    header_idx = next(
        (i for i, r in enumerate(rows) if sum(1 for c in r if _norm(c).lower() == "in l") >= 1), None
    )
    if header_idx is None:
        return False
    header = [_norm(c).lower() for c in rows[header_idx]]
    in_l = [i for i, h in enumerate(header) if h == "in l"]
    blocks = [(i - 1, "inputs") for i in in_l[:1]] + [(i - 1, "outputs") for i in in_l[1:2]]
    for start, which in blocks:
        for r in rows[header_idx + 1 :]:
            name = _norm(r[start]) if start < len(r) else ""
            if not name:
                continue
            entry = MassBalanceEntry(
                name=name,
                litres=_num(r[start + 1]),
                kg=_num(r[start + 2]),
                fat=_num(r[start + 3]),
                snf=_num(r[start + 4]),
            )
            (out.mass_balance_inputs if which == "inputs" else out.mass_balance_outputs).append(entry)
    out.files.append(f"{fname}:{ws.title} (mass balance)")
    return True


def _read_design_criteria(ws, fname: str, out: SourceData) -> bool:
    rows = list(ws.iter_rows(values_only=True))
    if not any("design criteria" in _norm(c).lower() for r in rows[:5] for c in r):
        return False
    if rows and _norm(rows[0][0]):
        out.plant_title = _norm(rows[0][0])
    for r in rows[3:]:
        text = " | ".join(_norm(c) for c in r if _norm(c) and not isinstance(c, (int, float)))
        if text:
            out.design_criteria.append(text)
    out.files.append(f"{fname}:{ws.title} (design criteria)")
    return True


def _read_equipment(ws, fname: str, out: SourceData) -> bool:
    rows = list(ws.iter_rows(values_only=True))
    hdr_i = None
    for i, r in enumerate(rows[:20]):
        cells = [_norm(c).upper() for c in r]
        if "DESCRIPTION" in cells and "CAPACITY" in cells and any(c.startswith("QTY") for c in cells):
            hdr_i = i
            break
    if hdr_i is None:
        return False
    cells = [_norm(c).upper() for c in rows[hdr_i]]
    c_desc = cells.index("DESCRIPTION")
    c_cap = cells.index("CAPACITY")
    c_qty = next(i for i, c in enumerate(cells) if c.startswith("QTY"))
    c_uom = cells.index("UOM") if "UOM" in cells else c_qty + 1
    section = ""
    for n, r in enumerate(rows[hdr_i + 1 :], start=hdr_i + 2):
        desc = _norm(r[c_desc]) if c_desc < len(r) else ""
        qty = _num(r[c_qty]) if c_qty < len(r) else None
        if not desc:
            continue
        if qty is None and desc.isupper():
            section = desc
            continue
        if qty is None:
            continue
        cap_raw = _norm(r[c_cap]) or None
        out.equipment_rows.append(
            EquipmentRow(
                section=section,
                description=desc,
                capacity_raw=cap_raw,
                capacity=parse_quantity(cap_raw),
                qty=qty,
                uom=_norm(r[c_uom]) or None,
                ref=f"{fname}:{ws.title}!R{n}",
            )
        )
    out.files.append(f"{fname}:{ws.title} (equipment)")
    return True


def load_workbooks(files: list[Path] | list[tuple[str, bytes]]) -> SourceData:
    """files: paths, or (filename, content) pairs. The filename appears in provenance refs."""
    out = SourceData()
    for f in files:
        name, src = (f.name, f) if isinstance(f, Path) else (f[0], io.BytesIO(f[1]))
        wb = openpyxl.load_workbook(src, data_only=True, read_only=True)
        found = False
        for ws in wb.worksheets:
            found |= _read_mass_balance(ws, name, out)
            found |= _read_design_criteria(ws, name, out)
            found |= _read_equipment(ws, name, out)
        if not found:
            out.warnings.append(f"{name}: no recognised mass-balance, design-criteria or equipment table")
    return out
