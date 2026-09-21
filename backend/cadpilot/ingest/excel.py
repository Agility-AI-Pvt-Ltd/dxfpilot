"""Read mass-balance and equipment/design-criteria workbooks into SourceData.

Tables are recognised without relying on fixed header words. For every table the reader tries, in
order, and records which method succeeded:

1. headers   — header cells matched against synonyms (standards/ingest.yaml), with typo tolerance
2. llm       — only if 1 found nothing: the LLM maps sheets/columns from a preview (any wording or
               language, or no header row at all)
3. content   — only for the equipment list, if 1 and 2 found nothing: columns inferred from the
               values themselves (long text = description, small integers = quantity, "40 KLPH" = capacity)

Every mapping, whichever method produced it, is checked against the cell values before it is used.
"""

from __future__ import annotations

import difflib
import io
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter
from pydantic import BaseModel, Field

from ..model.engineering import Quantity
from ..standards import get_standards


class MassBalanceEntry(BaseModel):
    name: str
    ref: str | None = None  # "<file>:<sheet>!R<row>"
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
    qty_raw: str | None = None
    uom: str | None = None
    ref: str  # "<file>:<sheet>!R<row>" — provenance for every derived object
    note: str = ""  # how a non-trivial value was read ("2 working + 1 standby", "range 15–20, design 20")
    flags: list[str] = Field(default_factory=list)  # still unresolved: shown to the reviewer, never dropped
    read_by: Literal["rules", "llm"] = "rules"


class TableOverride(BaseModel):
    """A reviewer's column mapping for one sheet. Always wins over automatic detection."""

    file: str
    sheet: str
    kind: Literal["equipment_list", "mass_balance"]
    header_row: int | None = None  # 1-based; None = no header row
    first_data_row: int | None = None  # 1-based; default: the row after the header
    columns: dict[str, str] = Field(default_factory=dict)  # field -> column letter
    capacity_unit: str | None = None


class TableDetection(BaseModel):
    """How one table was recognised — shown to the reviewer in the Design data panel."""

    file: str
    sheet: str
    kind: Literal["equipment_list", "mass_balance", "design_criteria"]
    method: Literal["headers", "llm", "content", "manual"]
    header_row: int | None = None
    columns: dict[str, str] = Field(default_factory=dict)  # field -> "D: DESCRIPTION"
    rows: int = 0


READER_VERSION = 3  # bump when reading changes; stored data from older readers is re-read on access


class SourceData(BaseModel):
    reader_version: int = 0
    files: list[str] = Field(default_factory=list)
    mass_balance_inputs: list[MassBalanceEntry] = Field(default_factory=list)
    mass_balance_outputs: list[MassBalanceEntry] = Field(default_factory=list)
    design_criteria: list[str] = Field(default_factory=list)
    design_criteria_refs: list[str] = Field(default_factory=list)  # "file:sheet!R5" for each line above
    plant_title_ref: str | None = None
    equipment_rows: list[EquipmentRow] = Field(default_factory=list)
    plant_title: str = ""
    warnings: list[str] = Field(default_factory=list)
    detections: list[TableDetection] = Field(default_factory=list)

    def flagged_rows(self) -> list[EquipmentRow]:
        return [r for r in self.equipment_rows if r.flags]

    def daily_intake_litres(self) -> float | None:
        total = sum(e.litres or 0 for e in self.mass_balance_inputs)
        return total or None


# ---- text and number helpers -------------------------------------------------------------------


def _norm(v: object) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def _key(text: str) -> str:
    """Normalised header text: lower case, punctuation removed, single spaces (any script)."""
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"[^\w\s/%³ऀ-ॿ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _num(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*(?:nos?\.?|sets?|units?|pcs|lot)?\s*$", v.strip(), re.IGNORECASE)
        if m:
            return float(m.group(1).replace(",", "."))
    return None


def _unit_table() -> dict[str, str]:
    table: dict[str, str] = {}
    for canonical, spellings in get_standards().ingest.get("units", {}).items():
        for s in spellings:
            table[_key(s).replace(" ", "")] = canonical
    return table


_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def parse_count(raw: object) -> tuple[float, str] | None:
    """Quantity cell → (number of units, note). '2', '3 Nos', '2W+1S' (=3), '2 (1W+1S)', '2+1', 'two'."""
    if raw is None or raw == "":
        return None
    if (n := _num(raw)) is not None:
        return n, ""
    t = _norm(raw).lower()
    m = re.fullmatch(r"(\d+)\s*w(?:orking)?\s*\+\s*(\d+)\s*(?:s|standby|f|future)\b.*", t)
    if m:
        w, s = int(m.group(1)), int(m.group(2))
        kind = "future" if re.search(r"\d\s*f", t) else "standby"
        return float(w + s), f"{w} working + {s} {kind}"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:nos?\.?|sets?|units?)?\s*[(\[](.+)[)\]]\s*", t)
    if m:
        return float(m.group(1)), m.group(2).strip()
    m = re.fullmatch(r"(\d+)\s*\+\s*(\d+)", t)
    if m:
        return float(int(m.group(1)) + int(m.group(2))), f"{m.group(1)} + {m.group(2)}"
    if t.rstrip(". ").split(" ")[0] in _WORDS:
        return float(_WORDS[t.rstrip(". ").split(" ")[0]]), ""
    return None


def parse_capacity(raw: object, default_unit: str | None = None) -> tuple[Quantity | None, str]:
    """Capacity cell → (quantity, note). Adds '2 × 20 KLPH', '20 m³/h @ 3 bar' and '15–20 KLPH' to the
    forms parse_quantity understands."""
    if raw is None or isinstance(raw, (int, float)):
        return parse_quantity(raw, default_unit), ""
    t = _norm(raw)
    note = ""
    if "@" in t:  # operating condition after '@'
        t, cond = t.split("@", 1)
        note = f"at {cond.strip()}"
    m = re.fullmatch(r"\s*(\d+)\s*[x×*]\s*(.+)", t, re.IGNORECASE)
    if m and (q := parse_quantity(m.group(2), default_unit)):
        return q, f"{m.group(1)} × {q} (capacity per unit)" + (f", {note}" if note else "")
    m = re.fullmatch(r"\s*(\d[\d.,]*)\s*(?:-|–|to)\s*(\d[\d.,]*)\s*(.*)", t, re.IGNORECASE)
    if m and (q := parse_quantity(f"{m.group(2)} {m.group(3)}", default_unit)):
        return q, f"range {m.group(1)}–{m.group(2)}, designed at the upper value" + (f", {note}" if note else "")
    return parse_quantity(t, default_unit), note


def parse_quantity(raw: object, default_unit: str | None = None) -> Quantity | None:
    """'40 KLPH', '100 KL', '200 Ltr.', '5 / 10 KLPH', '40 kl/hr', '1,500 LPH', '40 किलोलीटर प्रति घंटा'."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return Quantity(value=float(raw), unit=default_unit) if default_unit else None
    text = _norm(raw)
    m = re.match(r"^\s*(\d[\d,]*(?:\.\d+)?)\s*(?:/\s*[\d.]+\s*)?(.*?)\s*$", text)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    unit_text = _key(m.group(2)).replace(" ", "")
    if not unit_text:
        return Quantity(value=value, unit=default_unit) if default_unit else None
    units = _unit_table()
    unit = units.get(unit_text) or units.get(unit_text.rstrip("."))
    return Quantity(value=value, unit=unit) if unit else None


# ---- header matching -----------------------------------------------------------------------------


def _field_score(header: str, spec: dict) -> float:
    h = _key(header)
    if not h:
        return 0.0
    words = set(h.split())
    if any(_key(a) in words or _key(a) == h for a in spec.get("avoid", [])):
        return 0.0
    best = 0.0
    for syn in spec["synonyms"]:
        s = _key(syn)
        if h == s:
            return 3.0
        s_words = s.split()
        if all(w in words for w in s_words) and len(words) - len(s_words) <= 3:
            best = max(best, 2.0)
        elif len(s) >= 4 and difflib.SequenceMatcher(None, h, s).ratio() >= 0.86:
            best = max(best, 1.5)
    return best


def _header_unit(header: str) -> str | None:
    """'Capacity (KLPH)' → 'KLPH': a unit named in the header applies to bare numbers below it."""
    m = re.search(r"\(([^)]+)\)", header)
    if not m:
        return None
    return _unit_table().get(_key(m.group(1)).replace(" ", ""))


@dataclass
class _Sheet:
    file: str
    title: str
    rows: list[list[Any]]

    def cell(self, r: int, c: int) -> Any:
        row = self.rows[r] if r < len(self.rows) else []
        return row[c] if c < len(row) else None

    def label(self, c: int, header_row: int | None) -> str:
        text = _norm(self.cell(header_row, c)) if header_row is not None else ""
        return f"{get_column_letter(c + 1)}: {text}" if text else get_column_letter(c + 1)


def _best_columns(sheet: _Sheet, r: int, fields: dict) -> dict[str, tuple[int, float]]:
    """For one row: best column per field (distinct columns, highest score, leftmost on ties)."""
    cands = []
    for c, v in enumerate(sheet.rows[r]):
        if not isinstance(v, str):
            continue
        for f, spec in fields.items():
            s = _field_score(v, spec)
            if s:
                cands.append((s, -c, f, c))
    chosen: dict[str, tuple[int, float]] = {}
    used: set[int] = set()
    for s, _neg, f, c in sorted(cands, reverse=True):
        if f not in chosen and c not in used:
            chosen[f] = (c, s)
            used.add(c)
    return chosen


# ---- equipment list ------------------------------------------------------------------------------


@dataclass
class _EquipMap:
    description: int
    capacity: int | None
    quantity: int
    unit: int | None
    header_row: int | None
    first_row: int
    capacity_unit: str | None = None


def _extract_equipment(sheet: _Sheet, m: _EquipMap) -> list[EquipmentRow]:
    out: list[EquipmentRow] = []
    section = ""
    for r in range(m.first_row, len(sheet.rows)):
        desc = _norm(sheet.cell(r, m.description))
        if not desc or _num(desc) is not None:
            continue
        qty_cell = sheet.cell(r, m.quantity)
        cap_cell = sheet.cell(r, m.capacity) if m.capacity is not None else None
        qty_raw, cap_raw = _norm(qty_cell) or None, _norm(cap_cell) or None
        if qty_raw is None and cap_raw is None:
            section = desc  # a heading row: text but no quantity or capacity (any language)
            continue
        # Every other row is kept. What cannot be read is flagged for the LLM and the reviewer.
        count = parse_count(qty_cell)
        capacity, cap_note = parse_capacity(cap_cell, m.capacity_unit)
        flags: list[str] = []
        if count is None:
            flags.append(f"quantity not understood: '{qty_raw}'" if qty_raw else "quantity missing")
        if capacity is None and cap_raw and re.search(r"\d", cap_raw):
            flags.append(f"capacity not understood: '{cap_raw}'")
        out.append(
            EquipmentRow(
                section=section,
                description=desc,
                capacity_raw=cap_raw,
                capacity=capacity,
                qty=count[0] if count else None,
                qty_raw=qty_raw,
                uom=(_norm(sheet.cell(r, m.unit)) or None) if m.unit is not None else None,
                ref=f"{sheet.file}:{sheet.title}!R{r + 1}",
                note="; ".join(n for n in ((count[1] if count else ""), cap_note) if n),
                flags=flags,
            )
        )
    return out


def _plausible_equipment(rows: list[EquipmentRow]) -> bool:
    """Checks any proposed mapping against the data it would read."""
    if len(rows) < 3:
        return False
    with_capacity = sum(1 for r in rows if r.capacity is not None)
    with_qty = sum(1 for r in rows if r.qty is not None)
    long_text = sum(1 for r in rows if len(r.description) >= 6)
    return with_capacity >= 3 and with_qty >= 0.5 * len(rows) and long_text >= 0.8 * len(rows)


def _equipment_by_headers(sheet: _Sheet, spec: dict) -> _EquipMap | None:
    best: tuple[float, _EquipMap] | None = None
    for r in range(min(40, len(sheet.rows))):
        cols = _best_columns(sheet, r, spec["fields"])
        if not all(f in cols for f in spec["required"]):
            continue
        m = _EquipMap(
            description=cols["description"][0], capacity=cols["capacity"][0], quantity=cols["quantity"][0],
            unit=cols["unit"][0] if "unit" in cols else None, header_row=r, first_row=r + 1,
            capacity_unit=_header_unit(_norm(sheet.cell(r, cols["capacity"][0]))),
        )
        score = sum(s for _, s in cols.values())
        if best is None or score > best[0]:
            best = (score, m)
    return best[1] if best else None


_UOM_WORDS = {"nos", "no", "set", "sets", "mtr", "m", "lot", "each", "ea", "pcs", "unit", "units"}


def _equipment_by_content(sheet: _Sheet) -> _EquipMap | None:
    """No usable header: infer the columns from what the cells contain."""
    width = max((len(r) for r in sheet.rows), default=0)
    if width < 3:
        return None
    long_text, capacity, small_int, uom = [0] * width, [0] * width, [0] * width, [0] * width
    for row in sheet.rows:
        for c, v in enumerate(row):
            if isinstance(v, str):
                t = _norm(v)
                if parse_quantity(t):
                    capacity[c] += 1
                elif len(t) >= 12:
                    long_text[c] += 1
                elif _key(t).rstrip(".") in _UOM_WORDS:
                    uom[c] += 1
            if (n := _num(v)) is not None and n == int(n) and 0 < n <= 1000:
                small_int[c] += 1
    desc = max(range(width), key=lambda c: long_text[c])
    cap = max(range(width), key=lambda c: capacity[c])
    if long_text[desc] < 3 or capacity[cap] < 3 or cap == desc:
        return None
    qty_candidates = [c for c in range(width) if c not in (desc, cap) and small_int[c] >= 3]
    if not qty_candidates:
        return None
    qty = max(qty_candidates, key=lambda c: (small_int[c], -c))
    unit = max(range(width), key=lambda c: uom[c]) if max(uom) >= 3 else None
    first = next(
        (r for r in range(len(sheet.rows)) if len(_norm(sheet.cell(r, desc))) >= 6 and _num(sheet.cell(r, qty)) is not None),
        0,
    )
    return _EquipMap(description=desc, capacity=cap, quantity=qty, unit=unit, header_row=None, first_row=first)


# ---- mass balance --------------------------------------------------------------------------------


def _pct(v: float | None) -> float | None:
    """Fat/SNF as a fraction: 4 (%) and 0.04 both mean 4 %."""
    return v / 100 if v is not None and v > 1 else v


@dataclass
class _MBBlock:
    role: Literal["input", "output"]
    name: int
    litres: int | None
    kg: int | None
    fat: int | None
    snf: int | None


def _mass_balance_by_headers(sheet: _Sheet, spec: dict) -> tuple[int, list[_MBBlock]] | None:
    fields = spec["fields"]
    for r in range(min(40, len(sheet.rows))):
        scores = {
            c: {f: _field_score(v, s) for f, s in fields.items()}
            for c, v in enumerate(sheet.rows[r]) if isinstance(v, str)
        }
        litres_cols = sorted(c for c, s in scores.items() if s["litres"] >= 2 and s["litres"] >= max(s.values()))
        if not litres_cols or not any(s["fat"] >= 2 for s in scores.values()):
            continue
        blocks = []
        bounds = litres_cols + [len(sheet.rows[r])]
        for i, lc in enumerate(litres_cols):
            lo = litres_cols[i - 1] + 1 if i else 0
            hi = bounds[i + 1]

            def pick(field: str, start: int, end: int, near: int = lc) -> int | None:
                return max(
                    (c for c in range(start, end) if c in scores and scores[c][field] >= 2),
                    key=lambda c: (scores[c][field], -abs(c - near)), default=None,
                )

            name = pick("name", lo, lc)
            if name is None and lc > 0:
                name = lc - 1  # names sit just left of the volume column
            if name is None:
                continue
            blocks.append(
                _MBBlock(role="input" if i == 0 else "output", name=name, litres=lc,
                         kg=pick("kg", lc + 1, hi), fat=pick("fat", lc + 1, hi), snf=pick("snf", lc + 1, hi))
            )
        if blocks:
            return r, blocks
    return None


def _extract_mass_balance(sheet: _Sheet, first_row: int, blocks: list[_MBBlock]) -> tuple[list[MassBalanceEntry], list[MassBalanceEntry]]:
    inputs: list[MassBalanceEntry] = []
    outputs: list[MassBalanceEntry] = []
    for b in blocks:
        for r in range(first_row, len(sheet.rows)):
            name = _norm(sheet.cell(r, b.name))
            litres = _num(sheet.cell(r, b.litres)) if b.litres is not None else None
            kg = _num(sheet.cell(r, b.kg)) if b.kg is not None else None
            if not name or _num(name) is not None or (litres is None and kg is None):
                continue
            entry = MassBalanceEntry(
                name=name, litres=litres, kg=kg, ref=f"{sheet.file}:{sheet.title}!R{r + 1}",
                fat=_pct(_num(sheet.cell(r, b.fat))) if b.fat is not None else None,
                snf=_pct(_num(sheet.cell(r, b.snf))) if b.snf is not None else None,
            )
            (inputs if b.role == "input" else outputs).append(entry)
    return inputs, outputs


# ---- LLM table mapping ---------------------------------------------------------------------------


class _LLMEquipmentColumns(BaseModel):
    description: str = Field(description="Column letter holding the equipment description")
    capacity: str | None = Field(description="Column letter holding the capacity/size, or null")
    quantity: str = Field(description="Column letter holding the quantity (number of units)")
    unit: str | None = Field(description="Column letter holding the quantity's unit (Nos, Set), or null")
    capacity_unit: str | None = Field(description="Unit for bare capacity numbers if the sheet states it elsewhere (e.g. KLPH), else null")


class _LLMMassBalanceBlock(BaseModel):
    role: Literal["input", "output"]
    name: str = Field(description="Column letter with the milk/product names")
    litres: str | None
    kg: str | None
    fat: str | None
    snf: str | None


class _LLMSheetMap(BaseModel):
    file: str
    sheet: str
    kind: Literal["equipment_list", "mass_balance", "design_criteria", "other"]
    header_row: int | None = Field(description="1-based row number of the column headers; null if the table has none")
    first_data_row: int = Field(description="1-based row number of the first data row")
    equipment: _LLMEquipmentColumns | None
    mass_balance: list[_LLMMassBalanceBlock]


class _LLMWorkbookMap(BaseModel):
    sheets: list[_LLMSheetMap]


def _preview(sheets: list[_Sheet], max_rows: int = 22) -> str:
    parts = []
    for s in sheets:
        lines = [f"### file={s.file!r} sheet={s.title!r}"]
        for r, row in enumerate(s.rows[:max_rows]):
            cells = [f"{get_column_letter(c + 1)}={_norm(v)[:40]!r}" for c, v in enumerate(row) if _norm(v)]
            if cells:
                lines.append(f"R{r + 1}: " + " | ".join(cells[:12]))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _col(letter: str | None) -> int | None:
    if not letter or not re.fullmatch(r"[A-Za-z]{1,3}", letter.strip()):
        return None
    return column_index_from_string(letter.strip().upper()) - 1


def _llm_map(sheets: list[_Sheet], need: set[str]) -> _LLMWorkbookMap | None:
    from ..agents.llm import get_llm  # local import: ingestion must work without the LLM layer

    llm = get_llm()
    if not llm.enabled or not sheets:
        return None
    return llm.structured(
        system=(
            "You map spreadsheet tables for CadPilot, a P&ID drafting system for dairy plants. The workbooks may "
            "use any wording or language, and a table may have no header row. For each sheet decide what it is: "
            "an equipment list (one row per equipment item with description, capacity and quantity), a mass "
            "balance (milk/products with litres, kg, fat, SNF; inputs and outputs may sit side by side), design "
            "criteria text, or other (civil works, costs, areas...). Give column letters and 1-based row numbers "
            "exactly as shown in the preview."
        ),
        prompt=f"Tables still needed: {', '.join(sorted(need))}\n\n{_preview(sheets)}",
        schema=_LLMWorkbookMap,
        purpose="table_mapper",
    )


# ---- LLM row reader: values the rules could not read ------------------------------------------

CapacityUnit = Literal[
    "KLPH", "LPH", "m3/h", "kg/h", "KL", "L", "kg", "t", "t/h", "t/day", "CFM", "m3/min", "kW", "kVA", "HP", "TR",
    "CPH", "PPH", "per min",
]


class _LLMRowReading(BaseModel):
    ref: str = Field(description="The row reference exactly as given")
    quantity: float | None = Field(description="Number of units; null if the cell does not state one")
    capacity_value: float | None = Field(description="Capacity per unit as a number; null if not stated")
    capacity_unit: CapacityUnit | None = Field(description="Unit of capacity_value; null if none of these fits")
    note: str = Field(description="One short phrase on how you read it, e.g. '2 working + 1 standby'")


class _LLMRowReadings(BaseModel):
    rows: list[_LLMRowReading]


def _resolve_rows_with_llm(rows: list[EquipmentRow]) -> None:
    """One batched call for every flagged row. Only fills values the rules left empty; checks answers."""
    from ..agents.llm import get_llm

    todo = [r for r in rows if any(f.startswith(("quantity", "capacity")) for f in r.flags)][:80]
    llm = get_llm()
    if not todo or not llm.enabled:
        return
    listing = "\n".join(
        f"{r.ref} | {r.description[:100]} | capacity cell: {r.capacity_raw!r} | quantity cell: {r.qty_raw!r} | unit: {r.uom!r}"
        for r in todo
    )
    answer = llm.structured(
        system=(
            "You read awkward cells of a dairy-plant equipment list. For each row give the number of units and "
            "the capacity per unit if the cells state them (e.g. '2W+1S' = 3 units, 'one set' = 1, '2 x 20 KLPH' "
            "= 20 KLPH per unit, '20 cum/hr' = 20 m3/h). Never guess a value the cells do not contain: use null. "
            "Sizes like '76 mm x 6 Mtr.' or rates like 'tins/min' are not capacities in the allowed units: null."
        ),
        prompt=listing,
        schema=_LLMRowReadings,
        purpose="row_reader",
    )
    if answer is None:
        return
    by_ref = {r.ref: r for r in todo}
    for a in answer.rows:
        row = by_ref.get(a.ref)
        if row is None:
            continue
        fixed = []
        if row.qty is None and a.quantity is not None and 0 < a.quantity <= 10000:
            row.qty = float(a.quantity)
            fixed.append("quantity")
        if row.capacity is None and a.capacity_value is not None and a.capacity_value > 0 and a.capacity_unit:
            row.capacity = Quantity(value=float(a.capacity_value), unit=a.capacity_unit)
            fixed.append("capacity")
        if fixed:
            row.flags = [f for f in row.flags if not any(f.startswith(k) for k in fixed)]
            row.read_by = "llm"
            row.note = "; ".join(n for n in (row.note, f"read by AI: {a.note}") if n)


# ---- workbook loading ----------------------------------------------------------------------------


def _override_equipment_map(ov: TableOverride) -> _EquipMap | None:
    desc, qty = _col(ov.columns.get("description")), _col(ov.columns.get("quantity"))
    if desc is None or qty is None:
        return None
    hdr = ov.header_row - 1 if ov.header_row else None
    first = ov.first_data_row - 1 if ov.first_data_row else (hdr + 1 if hdr is not None else 0)
    unit = _unit_table().get(_key(ov.capacity_unit or "").replace(" ", "")) if ov.capacity_unit else None
    return _EquipMap(description=desc, capacity=_col(ov.columns.get("capacity")), quantity=qty,
                     unit=_col(ov.columns.get("unit")), header_row=hdr, first_row=first, capacity_unit=unit)


def _override_mass_balance(ov: TableOverride) -> tuple[int | None, int, list[_MBBlock]] | None:
    blocks = []
    for role in ("input", "output"):
        prefix = "" if role == "input" else "output_"
        name = _col(ov.columns.get(f"{prefix}name"))
        if name is None:
            continue
        blocks.append(_MBBlock(role=role, name=name, litres=_col(ov.columns.get(f"{prefix}litres")),  # type: ignore[arg-type]
                               kg=_col(ov.columns.get(f"{prefix}kg")), fat=_col(ov.columns.get(f"{prefix}fat")),
                               snf=_col(ov.columns.get(f"{prefix}snf"))))
    if not blocks:
        return None
    hdr = ov.header_row - 1 if ov.header_row else None
    first = ov.first_data_row - 1 if ov.first_data_row else (hdr + 1 if hdr is not None else 0)
    return hdr, first, blocks


def _find_sheet(sheets: list[_Sheet], file: str, title: str) -> _Sheet | None:
    return next((s for s in sheets if s.file == file and s.title == title), None) or next(
        (s for s in sheets if s.title == title), None
    )


def sheet_grids(files: list[Path] | list[tuple[str, bytes]], max_rows: int = 40, max_cols: int = 18) -> list[dict]:
    """The first rows/columns of every sheet as text, for the mapping editor."""
    out = []
    for s in _read_sheets(files):
        width = min(max((len(r) for r in s.rows[:max_rows]), default=0), max_cols)
        out.append({
            "file": s.file,
            "sheet": s.title,
            "columns": [get_column_letter(c + 1) for c in range(width)],
            "rows": [[_norm(s.cell(r, c))[:60] for c in range(width)] for r in range(min(max_rows, len(s.rows)))],
        })
    return out


def preview_override(files: list[Path] | list[tuple[str, bytes]], ov: TableOverride) -> dict:
    """What a proposed mapping would read — without saving it or calling the LLM."""
    d = load_workbooks(files, use_llm=False, overrides=[ov], only_overrides=True)
    if ov.kind == "equipment_list":
        rows = d.equipment_rows
        return {
            "total": len(rows),
            "with_quantity": sum(1 for r in rows if r.qty is not None),
            "with_capacity": sum(1 for r in rows if r.capacity is not None),
            "flagged": len(d.flagged_rows()),
            "rows": [r.model_dump() for r in rows[:12]],
            "warnings": d.warnings,
        }
    entries = d.mass_balance_inputs + d.mass_balance_outputs
    return {"total": len(entries), "rows": [e.model_dump() for e in entries[:12]], "warnings": d.warnings,
            "intake_litres": d.daily_intake_litres()}


def _read_sheets(files: list[Path] | list[tuple[str, bytes]]) -> list[_Sheet]:
    sheets: list[_Sheet] = []
    for f in files:
        name, src = (f.name, f) if isinstance(f, Path) else (f[0], io.BytesIO(f[1]))
        wb = openpyxl.load_workbook(src, data_only=True, read_only=True)
        for ws in wb.worksheets:
            sheets.append(_Sheet(file=name, title=ws.title, rows=[list(r) for r in ws.iter_rows(values_only=True)]))
    return sheets


def load_workbooks(
    files: list[Path] | list[tuple[str, bytes]],
    use_llm: bool = True,
    overrides: list[TableOverride] | None = None,
    only_overrides: bool = False,
) -> SourceData:
    """files: paths, or (filename, content) pairs. The filename appears in provenance refs.

    overrides: reviewer mappings — applied first and never second-guessed (only warned about).
    """
    cfg = get_standards().ingest
    out = SourceData(reader_version=READER_VERSION)
    sheets = _read_sheets(files)

    def add_equipment(sheet: _Sheet, m: _EquipMap, method: str) -> bool:
        rows = _extract_equipment(sheet, m)
        if method == "manual":
            if not rows:
                out.warnings.append(f"Your mapping of '{sheet.title}' reads no equipment rows — check the columns and rows.")
        elif not _plausible_equipment(rows):
            return False
        out.equipment_rows += rows
        cols = {"description": m.description, "capacity": m.capacity, "quantity": m.quantity, "unit": m.unit}
        columns = {f: sheet.label(c, m.header_row) for f, c in cols.items() if c is not None}
        if m.capacity_unit:
            columns["capacity unit"] = m.capacity_unit
        out.detections.append(TableDetection(
            file=sheet.file, sheet=sheet.title, kind="equipment_list", method=method,  # type: ignore[arg-type]
            header_row=m.header_row + 1 if m.header_row is not None else None, rows=len(rows), columns=columns,
        ))
        out.files.append(f"{sheet.file}:{sheet.title} (equipment)")
        return True

    def add_mass_balance(sheet: _Sheet, header_row: int | None, first_row: int, blocks: list[_MBBlock], method: str) -> bool:
        ins, outs = _extract_mass_balance(sheet, first_row, blocks)
        if not ins and method == "manual":
            out.warnings.append(f"Your mapping of '{sheet.title}' reads no mass-balance input rows.")
        elif not ins:
            return False
        out.mass_balance_inputs += ins
        out.mass_balance_outputs += outs
        cols = {}
        for b in blocks:
            for f in ("name", "litres", "kg", "fat", "snf"):
                c = getattr(b, f)
                if c is not None:
                    cols[f"{b.role} {f}"] = sheet.label(c, header_row)
        out.detections.append(TableDetection(
            file=sheet.file, sheet=sheet.title, kind="mass_balance", method=method,  # type: ignore[arg-type]
            header_row=header_row + 1 if header_row is not None else None, rows=len(ins) + len(outs), columns=cols,
        ))
        out.files.append(f"{sheet.file}:{sheet.title} (mass balance)")
        return True

    def add_design_criteria(sheet: _Sheet, method: str, start: int = 3) -> None:
        if sheet.rows and sheet.rows[0] and _norm(sheet.rows[0][0]):
            out.plant_title = _norm(sheet.rows[0][0])
            out.plant_title_ref = f"{sheet.file}:{sheet.title}!R1"
        before = len(out.design_criteria)
        for i, row in enumerate(sheet.rows[start:], start + 1):
            text = " | ".join(_norm(c) for c in row if _norm(c) and not isinstance(c, (int, float)))
            if text:
                out.design_criteria.append(text)
                out.design_criteria_refs.append(f"{sheet.file}:{sheet.title}!R{i}")
        out.detections.append(TableDetection(
            file=sheet.file, sheet=sheet.title, kind="design_criteria", method=method,  # type: ignore[arg-type]
            rows=len(out.design_criteria) - before,
        ))
        out.files.append(f"{sheet.file}:{sheet.title} (design criteria)")

    # 0. the reviewer's own mappings win
    manual: set[str] = set()
    for ov in overrides or []:
        sheet = _find_sheet(sheets, ov.file, ov.sheet)
        if sheet is None:
            out.warnings.append(f"Your mapping refers to sheet '{ov.sheet}' in {ov.file}, which is not in the uploaded workbooks.")
            continue
        if ov.kind == "equipment_list" and (em := _override_equipment_map(ov)):
            add_equipment(sheet, em, "manual")
            manual.add("equipment_list")
        elif ov.kind == "mass_balance" and (mbo := _override_mass_balance(ov)):
            add_mass_balance(sheet, mbo[0], mbo[1], mbo[2], "manual")
            manual.add("mass_balance")
    if only_overrides:
        return out

    # 1. headers / synonyms
    markers = [_key(m) for m in cfg["design_criteria"]["markers"]]
    for sheet in sheets:
        if "equipment_list" not in manual and (m := _equipment_by_headers(sheet, cfg["equipment"])) and add_equipment(sheet, m, "headers"):
            continue
        mb = _mass_balance_by_headers(sheet, cfg["mass_balance"]) if "mass_balance" not in manual else None
        if mb and add_mass_balance(sheet, mb[0], mb[0] + 1, mb[1], "headers"):
            continue
        if any(mk in _key(_norm(c)) for row in sheet.rows[:6] for c in row for mk in markers):
            add_design_criteria(sheet, "headers")

    # 2. LLM mapping for whatever is still missing (one call per upload, only when needed)
    need = {k for k, have in (("equipment_list", out.equipment_rows), ("mass_balance", out.mass_balance_inputs)) if not have} - manual
    if need and use_llm:
        mapped = _llm_map(sheets, need)
        for sm in mapped.sheets if mapped else []:
            sheet = next((s for s in sheets if s.title == sm.sheet and s.file == sm.file), None) or next(
                (s for s in sheets if s.title == sm.sheet), None
            )
            if sheet is None:
                continue
            hdr = sm.header_row - 1 if sm.header_row else None
            # read from just below the header so section headings are never skipped
            first = hdr + 1 if hdr is not None else max(sm.first_data_row - 1, 0)
            if sm.kind == "equipment_list" and "equipment_list" in need and sm.equipment:
                e = sm.equipment
                desc, qty = _col(e.description), _col(e.quantity)
                if desc is not None and qty is not None:
                    m = _EquipMap(
                        description=desc, capacity=_col(e.capacity), quantity=qty, unit=_col(e.unit),
                        header_row=hdr, first_row=first,
                        capacity_unit=_unit_table().get(_key(e.capacity_unit or "").replace(" ", "")),
                    )
                    if add_equipment(sheet, m, "llm"):
                        need.discard("equipment_list")
            elif sm.kind == "mass_balance" and "mass_balance" in need and sm.mass_balance:
                blocks = [
                    _MBBlock(role=b.role, name=n, litres=_col(b.litres), kg=_col(b.kg), fat=_col(b.fat), snf=_col(b.snf))
                    for b in sm.mass_balance if (n := _col(b.name)) is not None
                ]
                if blocks and add_mass_balance(sheet, hdr, first, blocks, "llm"):
                    need.discard("mass_balance")
            elif sm.kind == "design_criteria" and not out.design_criteria:
                add_design_criteria(sheet, "llm", start=first)

    # 3. content inference (equipment list only)
    if not out.equipment_rows and "equipment_list" not in manual:
        for sheet in sheets:
            if (m := _equipment_by_content(sheet)) and add_equipment(sheet, m, "content"):
                break

    # 4. awkward cells: rules first, then one batched LLM call; anything left stays flagged, never dropped
    if use_llm:
        _resolve_rows_with_llm(out.equipment_rows)
    if (n := len(out.flagged_rows())):
        out.warnings.append(f"{n} equipment row(s) need attention: kept, but a value could not be read.")

    if not out.equipment_rows:
        out.warnings.append("No equipment list recognised (needs description, capacity and quantity columns).")
    if not out.mass_balance_inputs:
        out.warnings.append("No mass balance recognised (needs product names with litres or kg).")
    return out
