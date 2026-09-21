"""Engineering modules: plant building blocks composed into one validated drawing."""

from __future__ import annotations

import os

os.environ["OPENAI_API_KEY"] = ""
os.environ["LANGSMITH_TRACING"] = "false"

import pytest  # noqa: E402

from cadpilot.agents import primary  # noqa: E402
from cadpilot.engine.dxf_export import svg_to_dxf  # noqa: E402
from cadpilot.service import CadPilot  # noqa: E402
from cadpilot.standards import get_standards  # noqa: E402
from cadpilot.store import FileStore  # noqa: E402

STD = get_standards()
ALL = list(STD.modules)
# a real inconsistency in the demo design data: 5 KLPH ghee pump feeding a 2 KLPH clarifier
KNOWN_DATA_WARNINGS = {"UNDERSIZED"}


@pytest.fixture()
def pilot(tmp_path):
    return CadPilot(FileStore(tmp_path))


def draw(pilot, modules, request="Generate the P&ID"):
    pid = pilot.create_project("modules", demo_data=True).info.id
    return pid, pilot.run_generation(pid, request, modules=modules)


def test_ten_modules_are_defined_with_their_own_rules():
    assert ALL == ["reception", "pasteurization", "cip", "steam", "hot_water", "chilled_water", "curd", "ghee", "cream", "packaging"]
    for m in STD.modules.values():
        assert m["stages"] and m["groups"] and m.get("summary")
    # module rules joined the shared libraries, tagged with their module
    assert any(r.get("module") == "steam" and r["id"] == "IR-ST-BOILER-LEVEL" for r in STD.instrumentation["rules"])
    assert any(r.get("module") == "steam" and r["id"] == "LR-ST-BOILER-NRV" for r in STD.line_rules["valves"])
    assert "boiler" in STD.symbols["equipment"] and STD.tagging["equipment"]["prefixes"]["boiler"] == "B"


@pytest.mark.parametrize("module", ALL)
def test_every_module_alone_is_a_valid_drawing(pilot, module):
    _pid, rev = draw(pilot, [module])
    assert rev.validation.passed, [i.message for i in rev.validation.errors]
    assert {i.code for i in rev.validation.warnings} <= KNOWN_DATA_WARNINGS, [i.message for i in rev.validation.warnings]
    assert {st.module for st in rev.model.process.stages} == {module}
    assert all(e.area == str(STD.modules[module]["area"]) for e in rev.model.equipment)


def test_whole_plant_is_valid_and_utilities_serve_every_consumer(pilot):
    pid, rev = draw(pilot, ALL)
    m = rev.model
    assert rev.validation.passed, [i.message for i in rev.validation.errors]
    assert m.counts()["equipment"] > 70 and m.counts()["control_loops"] > 40

    def outlets(stage):
        return [n.label for n in m.piping_nodes if n.stage == stage]

    def users(utility):
        return sorted(e.tag for e in m.equipment if utility in str(e.attributes.get("utility", "")).split(", "))

    for stage, utility in (("st_users", "steam"), ("hw_users", "hot water"), ("chw_users", "chilled water")):
        labels = outlets(stage)
        assert len(labels) == len(users(utility)) > 0
        assert all(any(tag in label for label in labels) for tag in users(utility))
    # every module is its own band: no two modules share a vertical range
    ys: dict[str, list[float]] = {}
    stage_mod = {st.id: st.module for st in m.process.stages}
    for e in m.equipment:
        ys.setdefault(stage_mod[e.stage], []).append(m.layout.positions[e.tag].y)
    bands = sorted((min(v), max(v)) for v in ys.values())
    assert all(a[1] < b[0] for a, b in zip(bands, bands[1:]))
    # the drawing and the DXF are produced for the whole plant
    assert "BOILER" in pilot.svg(pid, rev.revision).upper()
    assert len(svg_to_dxf(pilot.svg(pid, rev.revision))) > 100_000


def test_module_interface_carries_flow_and_names_the_other_area(pilot):
    _pid, rev = draw(pilot, ["reception", "pasteurization"])
    m = rev.model
    out = next(n for n in m.piping_nodes if n.stage == "rmst_outlet")
    assert out.label.endswith("(AREA 2)")
    silo_outlets = [ln for ln in m.lines if ln.from_.item.startswith("T-1")]
    assert silo_outlets and all(ln.size_dn and ln.design_flow for ln in silo_outlets)


def test_side_feed_uses_the_auxiliary_inlet_and_its_own_rules(pilot):
    _pid, rev = draw(pilot, ["cip"])
    m = rev.model
    feeds = [ln for ln in m.lines if ln.to.port == "aux_in"]
    tanks = {e.tag: e.name for e in m.equipment if e.type == "cip_tank"}
    assert {tanks[ln.to.item].split(" — ")[1] for ln in feeds} == {"Lye", "Acid"}
    assert all(any(c.provenance.rule == "LR-CIP-DOSING-NRV" for c in ln.inline) for ln in feeds)
    # the forward pump's own speed holds the circuit flow
    assert any(lp.final_element_kind == "vfd" and m.get_equipment(lp.final_element).stage == "cip_forward_pump" for lp in m.loops)


def test_steam_is_sized_as_steam_not_as_milk(pilot):
    _pid, rev = draw(pilot, ["steam"])
    steam = [ln for ln in rev.model.lines if ln.service == "ST" and ln.design_flow]
    assert steam and all(ln.design_flow.unit == "kg/h" and ln.spec == "S3" for ln in steam)
    main = max(steam, key=lambda ln: ln.design_flow.value)
    assert 100 <= main.size_dn <= 250  # ~16 t/h at ~25 m/s, not a DN of liquid velocity


def test_request_picks_modules_and_classic_requests_stay_classic():
    tid, _ = primary.choose_template("Draft the CIP station and the steam boilers", STD, "milk_reception_to_pasteurization")
    assert tid == "modules:cip+steam"
    tid, _ = primary.choose_template("Generate the P&ID for milk reception to pasteurization", STD, "milk_reception_to_pasteurization")
    assert tid == "milk_reception_to_pasteurization"
    tid, _ = primary.choose_template("Draw the whole plant", STD, "milk_reception_to_pasteurization")
    assert tid.count("+") == len(ALL) - 1
    # a module project keeps its scope on later requests
    tid, _ = primary.choose_template("add a silo", STD, "modules:cip+steam")
    assert tid == "modules:cip+steam"


def test_corrections_and_scope_changes_on_a_module_project(pilot):
    pid, rev = draw(pilot, ["cip"])
    pump = next(e.tag for e in rev.model.equipment if e.stage == "cip_forward_pump")
    out = pilot.chat(pid, f"Change {pump} to 50 KLPH")
    rev2 = pilot.store.revision(pid, out["revision"])
    assert rev2.model.process.template == "modules:cip" and str(rev2.model.get_equipment(pump).capacity) == "50 KLPH"
    rev3 = pilot.run_generation(pid, "Regenerate", modules=[])  # back to the classic line
    assert rev3.model.process.template == "milk_reception_to_pasteurization" and rev3.validation.passed


def test_unknown_module_is_refused(pilot):
    pid = pilot.create_project("x", demo_data=True).info.id
    with pytest.raises(ValueError, match="Unknown engineering module"):
        pilot.run_generation(pid, "x", modules=["brewery"])


def test_stage_ids_must_be_unique_across_modules(tmp_path, monkeypatch):
    from cadpilot import standards as std_mod

    (tmp_path / "modules").mkdir()
    (tmp_path / "modules" / "dup.yaml").write_text(
        "id: dup\nname: Dup\narea: '99'\narea_name: Dup\ngroups: {g1: {}}\n"
        "stages: [{id: cip_tank, name: x, kind: equipment, group: g1}]\n"
    )
    with pytest.raises(ValueError, match="both define stage 'cip_tank'"):
        std_mod._load((str(std_mod.BASELINE_DIR), str(tmp_path)))


def test_utility_takeoffs_get_isolation_not_route_valves_and_a_share_of_the_flow(pilot):
    _pid, rev = draw(pilot, ALL)
    m = rev.model
    takeoffs = [ln for ln in m.lines if ln.service == "ST" and m.get_equipment(ln.to.item) is None
                and any(n.tag == ln.to.item and n.kind == "terminal_out" for n in m.piping_nodes)]
    main = next(ln for ln in m.lines if ln.service == "ST" and ln.from_.item.startswith("PRV"))
    assert len(takeoffs) >= 3
    assert all(ln.size_dn < main.size_dn for ln in takeoffs)
    assert all({c.provenance.rule for c in ln.inline} == {"LR-ST-USER-ISOLATION"} for ln in takeoffs)
    for svc in ("ST", "CON", "HWS", "HWR", "CHWS", "CHWR"):  # no milk route valves on utility mains
        assert not any(c.provenance.rule == "LR-MANIFOLD-ROUTE" for ln in m.lines if ln.service == svc for c in ln.inline)
    assert any("equal share of the header flow" in d["detail"] for d in m.metadata["decisions"])
