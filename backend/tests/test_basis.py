"""Design basis: disputed numbers go to a human, the approved value drives the solver, and every
value on the drawing can be traced back to its source row."""

from __future__ import annotations

import os

os.environ["OPENAI_API_KEY"] = ""
os.environ["LANGSMITH_TRACING"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cadpilot.api import main  # noqa: E402
from cadpilot.service import CadPilot  # noqa: E402
from cadpilot.store import FileStore  # noqa: E402

CLASSIC = "Generate the P&ID for milk reception to pasteurization"


@pytest.fixture()
def pilot(tmp_path):
    return CadPilot(FileStore(tmp_path))


@pytest.fixture()
def pid(pilot):
    return pilot.create_project("basis", demo_data=True).info.id


def test_conflicts_are_found_with_every_source_and_row(pilot, pid):
    b = pilot.design_basis(pid)
    cap = b.get("plant_capacity")
    assert cap.status == "conflict" and cap.value is None
    got = {(c.source, c.value, c.ref.split("!")[-1]) for c in cap.candidates}
    assert ("Mass balance", 500_000, "R4") in got and ("Design criteria", 1_000_000, "R7") in got
    assert b.get("processing_line_klph").status == "conflict"  # 2 × 30 KLPH (criteria) vs 20 KLPH (cost estimate)
    assert {c.value for c in b.get("packing_machines").candidates} == {16, 4}


def test_generation_waits_for_the_human_then_uses_the_approved_basis(pilot, pid, monkeypatch):
    monkeypatch.setattr(main, "_pilot", pilot)
    client = TestClient(main.app)
    res = client.post(f"/api/projects/{pid}/generate", json={"request": CLASSIC})
    assert res.status_code == 409  # no silent choice
    ids = {p["id"] for p in res.json()["conflicts"]}
    assert ids == {"plant_capacity", "processing_line_klph"}  # only what the classic line depends on
    assert pilot.store.get(pid).current is None  # nothing was drafted

    cap = pilot.design_basis(pid).get("plant_capacity")
    crit = next(c for c in cap.candidates if c.value == 1_000_000)
    line = next(c for c in pilot.design_basis(pid).get("processing_line_klph").candidates if c.source == "Cost estimate")
    client.post(f"/api/projects/{pid}/basis", json={"approvals": {
        "plant_capacity": {"value": crit.value, "source": crit.source, "ref": crit.ref, "note": "design for the 10 LLPD expansion"},
        "processing_line_klph": {"value": line.value, "source": line.source, "ref": line.ref},
    }}).raise_for_status()
    res = client.post(f"/api/projects/{pid}/generate", json={"request": CLASSIC})
    assert res.status_code == 200 and "event: result" in res.text

    rev = pilot.store.revision(pid, pilot.store.get(pid).current)
    count = lambda st: sum(1 for e in rev.model.equipment if e.stage == st)  # noqa: E731
    # 1,000,000 L/day: ÷ 8 h = 125 KLPH ÷ 40 → 4 pumps; ½ day = 500 KL ÷ 100 → 5 silos; ÷ 20 h = 50 KLPH ÷ 20 → 3 pasteurizers
    assert (count("unloading_pump"), count("raw_milk_silo"), count("pasteurizer")) == (4, 5, 3)
    assert not any(i.code == "BASIS_CONFLICT" for i in rev.validation.issues)
    chat = pilot.store.get(pid).chat
    note = next(m.text for m in chat if m.data.get("kind") == "basis")
    assert "1,000,000 L/day — approved by reviewer (Design criteria, Design criteria!R7) — design for the 10 LLPD expansion" in note


def test_provisional_draft_is_flagged(pilot, pid, monkeypatch):
    monkeypatch.setattr(main, "_pilot", pilot)
    res = TestClient(main.app).post(f"/api/projects/{pid}/generate", json={"request": CLASSIC, "provisional": True})
    assert res.status_code == 200
    rev = pilot.store.revision(pid, pilot.store.get(pid).current)
    assert any(i.code == "BASIS_CONFLICT" and "no value is approved" in i.message for i in rev.validation.warnings)


def test_every_value_traces_back_to_its_source(pilot, pid):
    cap = pilot.design_basis(pid).get("plant_capacity").candidates[0]
    pilot.approve_basis(pid, {"plant_capacity": {"value": cap.value, "source": cap.source, "ref": cap.ref}})
    rev = pilot.run_generation(pid, CLASSIC)
    pasteurizer = next(e.tag for e in rev.model.equipment if e.stage == "pasteurizer")
    t = pilot.trace(pid, rev.revision, pasteurizer)
    steps = {s.label: s for s in t.steps}
    assert steps["Capacity"].value == "20 KLPH" and steps["Capacity"].source.row == 20
    assert steps["Capacity"].source.sheet == "Eqpt. Cost Est." and "Milk Pasteurizer" in steps["Capacity"].source.text
    assert steps["Calculation"].value.startswith("25.00 KLPH ÷ 20 KLPH per unit = 1.25 → ⌈1.25⌉ = 2")
    assert steps["Design basis"].detail.startswith("approved by the reviewer") and steps["Design basis"].source.row == 4
    line = rev.model.lines[2]
    lt = {s.label: s for s in pilot.trace(pid, rev.revision, line.tag).steps}
    assert lt["Pipe size"].value == f"DN{line.size_dn}" and "m/s" in lt["Pipe size"].detail


def test_unknown_parameter_and_bad_value_are_refused(pilot, pid):
    with pytest.raises(ValueError, match="Unknown design parameter"):
        pilot.approve_basis(pid, {"brewing_capacity": {"value": 1, "source": "x"}})
    with pytest.raises(ValueError, match="positive"):
        pilot.approve_basis(pid, {"plant_capacity": {"value": 0, "source": "x"}})
