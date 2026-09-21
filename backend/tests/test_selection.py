"""Equipment selection: the number of units is calculated from the mass balance, not chosen by the AI."""

from __future__ import annotations

import os

os.environ["OPENAI_API_KEY"] = ""
os.environ["LANGSMITH_TRACING"] = "false"

import pytest  # noqa: E402

from cadpilot.engine import selection  # noqa: E402
from cadpilot.ingest.excel import MassBalanceEntry  # noqa: E402
from cadpilot.model.engineering import Quantity  # noqa: E402
from cadpilot.service import CadPilot  # noqa: E402
from cadpilot.store import FileStore  # noqa: E402


@pytest.fixture()
def pilot(tmp_path):
    return CadPilot(FileStore(tmp_path))


def sizing_of(rev) -> dict[str, dict]:
    return {r["stage"]: r for r in rev.model.metadata["sizing"]}


def count(rev, stage: str) -> int:
    return sum(1 for e in rev.model.equipment if e.stage == stage)


def test_units_are_mass_balance_divided_by_unit_capacity_rounded_up(pilot):
    pid = pilot.create_project("sizing", demo_data=True).info.id
    rev = pilot.run_generation(pid, "Generate the P&ID for milk reception to pasteurization")
    s = sizing_of(rev)
    # 500,000 L/day in 8 h = 62.5 KLPH ÷ 40 KLPH = 1.56 → 2 unloading pumps
    assert s["unloading_pump"]["required"] == "62.50 KLPH" and s["unloading_pump"]["units"] == 2 == count(rev, "unloading_pump")
    # half a day's intake = 250 KL ÷ 100 KL = 2.5 → 3 silos
    assert s["raw_milk_silo"]["required"] == "250.00 KL" and s["raw_milk_silo"]["units"] == 3 == count(rev, "raw_milk_silo")
    # 500,000 L in 20 h = 25 KLPH ÷ 20 KLPH = 1.25 → 2 pasteurizers
    assert s["pasteurizer"]["ratio"] == 1.25 and s["pasteurizer"]["units"] == 2 == count(rev, "pasteurizer")


def test_the_drawing_follows_the_mass_balance_not_the_cost_estimate(pilot, monkeypatch):
    pid = pilot.create_project("double intake", demo_data=True).info.id
    source = pilot.store.source_data(pid)
    doubled = source.model_copy(update={"mass_balance_inputs": [
        e.model_copy(update={"litres": e.litres * 2 if e.litres else None, "kg": e.kg * 2 if e.kg else None})
        for e in source.mass_balance_inputs]})
    monkeypatch.setattr(pilot.store, "source_data", lambda _pid: doubled)
    rev = pilot.run_generation(pid, "Generate the P&ID for milk reception to pasteurization")
    # 125 KLPH ÷ 40 = 3.1 → 4 pumps; 500 KL ÷ 100 = 5 silos; 50 KLPH ÷ 20 = 2.5 → 3 pasteurizers
    assert (count(rev, "unloading_pump"), count(rev, "raw_milk_silo"), count(rev, "pasteurizer")) == (4, 5, 3)
    warn = [i.message for i in rev.validation.warnings if i.code == "QTY_DIFFERS_FROM_DESIGN_DATA"]
    assert any("Tanker unloading pump: the mass balance needs 4 unit(s)" in w and "design data lists 2" in w for w in warn)


def test_module_sizing_for_products(pilot):
    pid = pilot.create_project("products", demo_data=True).info.id
    rev = pilot.run_generation(pid, "x", modules=["curd", "paneer", "ghee", "packaging", "powder"])
    s = sizing_of(rev)
    assert s["pk_filler"]["units"] == 4  # 130,000 L × 2 pouches/L ÷ 8 h = 32,500 PPH ÷ 10,000 → 4
    assert s["cu_pouch_filler"]["units"] == 3 and "one per product" in s["cu_pouch_filler"]["note"]  # curd pouch, lassi, butter-milk
    assert s["pn_vat"]["units"] == 3  # 2,000 kg × 5 L/kg in 4 batches = 2.5 KL ÷ 1 KL → 3
    assert s["pk_rechiller"]["units"] == 2 and s["pk_rechiller"]["standby"] == 1
    assert s["pw_dryer"]["units"] == 1 and s["pw_dryer"]["required"] == "29.13 t/day"  # vs the 30 MTPD package
    assert "Surplus Butter" not in s["cu_silo"]["basis"]  # only the fermented products size the curd silos


def test_no_basis_keeps_the_design_data_quantity_and_says_so():
    stage = {"id": "x", "name": "X", "group": "g"}
    rec = selection.size_stage({"stage": "x", "basis": "products", "products": ["ice cream"], "hours": 8}, stage,
                               Quantity(value=5, unit="KLPH"), None, type("S", (), {"mass_balance_outputs": []})())
    assert rec.units == 0 and "design-data quantity is kept" in rec.note


def test_ceil_standby_and_units():
    stage = {"id": "p", "name": "Pump", "group": "g"}
    src = type("S", (), {"daily_intake_litres": lambda self: 400_000.0, "mass_balance_outputs": []})()
    rec = selection.size_stage({"stage": "p", "basis": "intake", "hours": 10, "standby": 1}, stage, Quantity(value=20, unit="KLPH"), None, src)
    assert (rec.required, rec.ratio, rec.working, rec.standby, rec.units) == ("40.00 KLPH", 2.0, 2, 1, 3)  # exactly 2, not 3
    assert MassBalanceEntry  # the ingest model is what the solver reads
