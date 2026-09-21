"""Scope agent: the plant sections a project's workbooks contain, with the rows as evidence."""

from __future__ import annotations

import os

os.environ["OPENAI_API_KEY"] = ""
os.environ["LANGSMITH_TRACING"] = "false"

import pytest  # noqa: E402

from cadpilot.agents import scope  # noqa: E402
from cadpilot.service import CadPilot  # noqa: E402
from cadpilot.standards import get_standards  # noqa: E402
from cadpilot.store import FileStore  # noqa: E402

STD = get_standards()


@pytest.fixture()
def pilot(tmp_path):
    return CadPilot(FileStore(tmp_path))


@pytest.fixture()
def demo(pilot):
    pid = pilot.create_project("scope", demo_data=True).info.id
    return pid, pilot.store.source_data(pid)


def test_demo_workbooks_contain_nine_sections_with_row_evidence(demo):
    _pid, source = demo
    det = scope.detect(source, STD)
    status = {m.id: m.status for m in det.modules}
    assert det.selected == ["reception", "pasteurization", "cip", "steam", "chilled_water", "curd", "ghee", "cream", "packaging"]
    assert status["hot_water"] == "partial"
    refs = {r.ref for r in source.equipment_rows}
    for m in det.modules:
        assert all(e.ref in refs for e in m.evidence)
        assert len({e.ref for e in m.evidence}) == len(m.evidence)  # one row counts once
    steam = next(m for m in det.modules if m.id == "steam")
    assert any("blow down" in e.text.lower() and e.capacity.strip() == "8 TPH" for e in steam.evidence)  # not the ghee boiler


def test_generation_detects_builds_and_tells_the_reviewer(pilot, demo):
    pid, _ = demo
    rev = pilot.run_generation(pid, "Generate the P&ID from the design data", detect_modules=True)
    assert rev.validation.passed
    assert rev.model.process.template.startswith("modules:") and "hot_water" not in rev.model.process.template
    rec = pilot.store.get(pid)
    note = next(m.text for m in rec.chat if m.data.get("kind") == "scope")
    assert note.startswith("I read your workbooks and found 9 plant sections")
    assert "R280 Boiler with auto blow down" in note and "Design data" in note
    assert "tick Hot water" in note  # needed by the pasteurizers, only partly in the workbooks
    assert rec.intent.scope_detection["selected"] == STD.template(rev.model.process.template)["modules"]


def test_a_request_that_names_sections_narrows_the_detection(pilot, demo):
    pid, _ = demo
    rev = pilot.run_generation(pid, "Only the CIP station and the steam boilers", detect_modules=True)
    assert rev.model.process.template == "modules:cip+steam"


def test_nothing_recognisable_falls_back_to_the_classic_line(pilot, demo, monkeypatch):
    pid, source = demo
    empty = source.model_copy(update={"equipment_rows": []})
    det = scope.detect(empty, STD)
    assert det.selected == [] and all(m.status == "not_found" for m in det.modules)
    monkeypatch.setattr(pilot.store, "source_data", lambda _pid: empty)
    chosen, note = pilot._detect_scope(pid, "Generate the P&ID from the design data", None)
    assert chosen == [] and "classic" in note


def test_the_ai_can_add_a_section_only_with_real_rows(demo, monkeypatch):
    _pid, source = demo
    real = next(r.ref for r in source.equipment_rows if "hot water generation" in r.description.lower())

    class FakeLLM:
        enabled = True
        model = "fake"

        def structured(self, system, prompt, schema, purpose, **_):
            return schema(present=[
                scope._AIModule(module_id="hot_water", row_refs=[real, "Eqpt!R99999"], reason="hot water generation system listed"),
                scope._AIModule(module_id="brewery", row_refs=[real], reason="made up"),
                scope._AIModule(module_id="packaging", row_refs=["Eqpt!R0"], reason="no real row"),
            ])

    monkeypatch.setattr(scope, "get_llm", lambda: FakeLLM())
    det = scope.detect(source, STD)
    hot = next(m for m in det.modules if m.id == "hot_water")
    assert hot.status == "found" and hot.found_by == "ai" and [e.ref for e in hot.evidence] == [real]
    assert "hot_water" in det.selected and det.used_llm
    assert next(m for m in det.modules if m.id == "packaging").found_by == "rows"  # rows already proved it
