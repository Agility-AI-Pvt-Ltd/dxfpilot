from __future__ import annotations

import os
import uuid

import pytest

os.environ["OPENAI_API_KEY"] = ""  # tests run on the rule agents; .env does not override this

from cadpilot import config  # noqa: E402,F401  (loads DATABASE_URL from backend/.env if present)
from cadpilot.agents.interpreter import interpret_rules  # noqa: E402
from cadpilot.engine.validate import validate  # noqa: E402
from cadpilot.model.intent import AddBypass, Explain, ReorderStage, SetCapacity, SetGroupCount  # noqa: E402
from cadpilot.service import CadPilot  # noqa: E402
from cadpilot.store import FileStore  # noqa: E402

DB_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


@pytest.fixture(params=["file", "postgres"])
def store(request, tmp_path):
    if request.param == "file":
        yield FileStore(tmp_path)
        return
    if not DB_URL:
        pytest.skip("no DATABASE_URL / TEST_DATABASE_URL")
    import psycopg
    from psycopg import sql

    from cadpilot.pg_store import PostgresStore

    schema = f"test_{uuid.uuid4().hex[:10]}"
    try:
        s = PostgresStore(DB_URL, schema=schema, max_size=2)
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    yield s
    s.close()
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture()
def pilot(store):
    return CadPilot(store)


@pytest.fixture()
def project(pilot):
    rec = pilot.create_project("Test dairy", demo_data=True)
    rev = pilot.run_generation(rec.info.id, "Generate P&ID for milk reception to pasteurization")
    return rec.info.id, rev


def test_excel_sources_loaded(pilot):
    rec = pilot.create_project("x", demo_data=True)
    src = pilot.store.source_data(rec.info.id)
    assert src.daily_intake_litres() == 500000
    assert any("Milk Pasteurizer" in r.description for r in src.equipment_rows)


def test_autonomous_first_draft_is_complete_and_valid(project):
    _, rev = project
    m = rev.model
    assert rev.status == "ready_for_review"
    assert rev.validation.passed, [i.message for i in rev.validation.errors]
    assert not [i for i in rev.validation.warnings if i.code == "UNDERSIZED"]
    c = m.counts()
    assert c["equipment"] == 19  # 2 reception trains × 4, 3 silos, 2 processing trains × 4
    assert c["control_loops"] == 10
    # Every equipment item is traceable to design data
    assert all(e.provenance.source for e in m.equipment)
    # Chain order in a processing train
    chain = []
    cur = "P-201"
    while cur and not cur.startswith("BL"):
        chain.append(cur)
        cur = next(ln.to.item for ln in m.lines if ln.from_.item == cur)
    assert chain == ["P-201", "F-201", "S-201", "E-201"]
    assert set(m.layout.positions) == m.node_tags()


def test_revision_svg_renders(pilot, project):
    pid, rev = project
    svg = pilot.svg(pid, rev.revision)
    assert svg.startswith("<svg") and 'data-tag="E-201"' in svg and "FDV-" in svg


def test_rule_parser():
    assert isinstance(interpret_rules("The separator should be before the pasteurizer.", None).operations[0], ReorderStage)
    op = interpret_rules("Change E-101 to 50 KLPH", None).operations[0]
    assert isinstance(op, SetCapacity) and op.tag == "E-101" and op.value == 50
    assert isinstance(interpret_rules("Add a bypass", "F-201").operations[0], AddBypass)
    assert isinstance(interpret_rules("Why is this instrument required?", "FT-103").operations[0], Explain)
    assert interpret_rules("make it prettier", None).clarification


def test_reorder_correction_creates_revision_and_preserves_tags(pilot, project):
    pid, rev_a = project
    out = pilot.chat(pid, "The pasteurizer should be before the separator")
    assert out["kind"] == "revision" and out["revision"] == "B"
    rev_b = pilot.store.revision(pid, "B")
    # topology changed ...
    assert any(ln.from_.item == "E-201" and ln.to.item == "S-201" for ln in rev_b.model.lines)
    # ... tags of unchanged equipment preserved, most of the model untouched
    assert {e.tag for e in rev_a.model.equipment} == {e.tag for e in rev_b.model.equipment}
    assert rev_b.diff.lines.unchanged > 10
    # the validator flags the engineering-order deviation as a warning, not silently
    assert any(i.code == "ORDER_CONSTRAINT" for i in rev_b.validation.warnings)
    # ... and correcting it back returns to the original line topology
    pilot.chat(pid, "The separator should be before the pasteurizer")
    rev_c = pilot.store.revision(pid, "C")
    assert sorted((ln.from_.item, ln.to.item) for ln in rev_c.model.lines) == sorted(
        (ln.from_.item, ln.to.item) for ln in rev_a.model.lines
    )


def test_capacity_correction_propagates_and_is_validated(pilot, project):
    pid, _ = project
    pilot.chat(pid, "Change E-101 to 30 KLPH")
    rev = pilot.store.revision(pid, "B")
    assert str(rev.model.get_equipment("E-101").capacity) == "30 KLPH"
    assert rev.diff.equipment.modified == {"E-101": ["capacity"]}
    # 30 KLPH chiller on a 40 KLPH reception line → data validation warning
    assert any(i.code == "UNDERSIZED" and "E-101" in i.refs for i in rev.validation.warnings)


def test_bypass_and_remove_instrument(pilot, project):
    pid, _ = project
    pilot.chat(pid, "Add a bypass", selected="F-201")
    rev = pilot.store.revision(pid, "B")
    assert any(ln.kind == "bypass" for ln in rev.model.lines)
    assert rev.validation.passed, [i.message for i in rev.validation.errors]

    pilot.chat(pid, "Remove LSHH-111")
    rev_c = pilot.store.revision(pid, "C")
    assert rev_c.model.get_instrument("LSHH-111") is None
    assert any(i.code == "RULE_WAIVED" for i in rev_c.validation.issues)
    assert rev_c.validation.passed


def test_questions_do_not_create_revisions(pilot, project):
    pid, _ = project
    out = pilot.chat(pid, "Why is FT-103 required?")
    assert out["kind"] == "answer" and "IR-RECEPTION-FLOW" in out["reply"]
    assert pilot.store.get(pid).revisions == ["A"]


def test_required_stage_cannot_be_removed(pilot, project):
    pid, _ = project
    out = pilot.chat(pid, "Remove the pasteurizer")
    assert out["kind"] == "answer" and "required" in out["reply"]


def test_validator_catches_broken_model(project):
    from cadpilot.standards import get_standards

    _, rev = project
    m = rev.model.model_copy(deep=True)
    m.lines = [ln for ln in m.lines if ln.to.item != "E-201"]
    m.instruments = [i for i in m.instruments if i.function != "LSHH"]
    codes = {i.code for i in validate(m, get_standards()).errors}
    assert {"NO_INLET", "MISSING_INSTRUMENT", "NO_PROCESS_PATH"} <= codes


def test_approval(pilot, project):
    pid, rev = project
    assert pilot.approve(pid, rev.revision).status == "approved"


def test_capacity_change_on_non_driving_unit_does_not_resize_lines(pilot, project):
    pid, _ = project
    pilot.chat(pid, "Change E-201 to 25 KLPH")
    rev = pilot.store.revision(pid, "B")
    assert rev.diff.equipment.modified == {"E-201": ["capacity"]}
    assert not rev.diff.lines.modified and not rev.validation.warnings


def test_pump_capacity_drives_its_train_only(pilot, project):
    pid, _ = project
    pilot.chat(pid, "Change P-201 to 25 KLPH")
    rev = pilot.store.revision(pid, "B")
    changed = set(rev.diff.lines.modified)
    assert changed and all(pilot.store.revision(pid, "B").model.get_line(t).design_flow.value in (25, 45) for t in changed)
    assert not any("2002" in t or "2004" in t or "2006" in t for t in changed)  # train 2 untouched
    assert any(i.code == "UNDERSIZED" and "E-201" in i.refs for i in rev.validation.warnings)


def test_upload_endpoint_replaces_by_role(pilot, monkeypatch):
    from fastapi.testclient import TestClient

    from cadpilot.api import main
    from cadpilot.service import DEMO_FILES

    monkeypatch.setattr(main, "_pilot", pilot)
    client = TestClient(main.app)
    pid = client.post("/api/projects", json={"name": "upload test"}).json()["id"]
    mb, dd = DEMO_FILES["mass_balance"], DEMO_FILES["design_data"]
    files = {
        "mass_balance": (mb.name, mb.read_bytes()),
        "design_data": (dd.name, dd.read_bytes()),
    }
    view = client.post(f"/api/projects/{pid}/sources", files=files).json()
    assert view["sources"] == {"mass_balance": mb.name, "design_data": dd.name}
    assert view["source_summary"]["equipment_rows"] == 295
    # re-uploading one role replaces it rather than doubling the intake
    client.post(f"/api/projects/{pid}/sources", files={"mass_balance": files["mass_balance"]})
    src = main.pilot().store.source_data(pid)
    assert src.daily_intake_litres() == 500000
    assert src.equipment_rows[0].ref.startswith(dd.name)
    assert client.post(f"/api/projects/{pid}/sources", files={"mass_balance": ("x.csv", b"a")}).status_code == 400


def test_legacy_project_keeps_other_workbook_when_one_is_replaced(tmp_path):
    import json

    from cadpilot.service import DEMO_FILES

    pilot = CadPilot(FileStore(tmp_path))
    rec = pilot.store.create("legacy")
    pid = rec.info.id
    d = pilot.store.root / pid
    for p in DEMO_FILES.values():  # old layout: original filenames, list of names
        (d / "sources" / p.name).write_bytes(p.read_bytes())
    raw = json.loads((d / "project.json").read_text())
    raw["sources"] = [p.name for p in DEMO_FILES.values()]
    (d / "project.json").write_text(json.dumps(raw))

    mb = DEMO_FILES["mass_balance"]
    pilot.set_sources(pid, {"mass_balance": (mb.name, mb.read_bytes())})
    src = pilot.store.source_data(pid)
    assert src.daily_intake_litres() == 500000 and len(src.equipment_rows) == 295


def test_history_survives_a_fresh_store_instance(pilot, project):
    """Everything needed to resume a project is persisted: revisions, chat, corrections, tags."""
    pid, _ = project
    pilot.chat(pid, "Change E-201 to 25 KLPH")
    pilot.approve(pid, "B")
    store = pilot.store
    reopened = store.__class__(store.root) if isinstance(store, FileStore) else None
    rec = (reopened or store).get(pid)
    assert rec.revisions == ["A", "B"]
    assert [m.role for m in rec.chat] == ["user", "assistant", "user", "assistant", "assistant"]
    assert rec.corrections[0].operations[0].op == "set_capacity"
    assert (reopened or store).revision(pid, "A").status == "superseded"
    b = (reopened or store).revision(pid, "B")
    assert b.status == "approved" and b.diff.equipment.modified == {"E-201": ["capacity"]}
    assert rec.registry.assigned  # tag registry persisted, so future revisions keep tags
    assert [p.id for p in store.list_projects()] == [pid] and store.list_projects()[0].current == "B"


def test_llm_reply_that_ignores_the_schema_falls_back_to_rules(pilot, project, monkeypatch):
    """A model that answers in prose instead of JSON must not break the chat."""
    from cadpilot.agents import interpreter, llm
    from cadpilot.agents.interpreter import _LLMInterpretation

    class ProseClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def parse(**_kw):
                    _LLMInterpretation.model_validate_json("The reviewer's request could mean ...")

    fake = llm.LLM()
    fake._client = ProseClient()
    monkeypatch.setattr(interpreter, "get_llm", lambda: fake)
    pid, _ = project
    out = pilot.chat(pid, "use 4 silots")
    assert out["kind"] == "revision"
    assert out["change_request"]["interpreter"] == "rules (model reply unusable)"
    assert len([e for e in pilot.store.revision(pid, "B").model.equipment if e.stage == "raw_milk_silo"]) == 4


def test_llm_clarification_is_overridden_when_rules_understand(pilot, project, monkeypatch):
    """A model that asks an unnecessary question (e.g. 'which tag for the 4th silo?') must not block a clear request."""
    from cadpilot.agents import interpreter, llm
    from cadpilot.agents.interpreter import _LLMInterpretation

    fake = llm.LLM()
    fake._client = object()
    monkeypatch.setattr(
        fake, "structured",
        lambda *a, **k: _LLMInterpretation(operations=[], clarification="Please specify the tag ID (e.g. T-104) for the fourth silo."),
    )
    monkeypatch.setattr(interpreter, "get_llm", lambda: fake)
    pid, _ = project
    out = pilot.chat(pid, "Use 4 silos")
    assert out["kind"] == "revision" and out["change_request"]["interpreter"] == "rules (model asked a question instead)"
    assert "T-104" in {e.tag for e in pilot.store.revision(pid, "B").model.equipment}
    # a genuinely unclear message still gets the model's question
    assert pilot.chat(pid, "make it better")["kind"] == "clarification"


def test_llm_calls_are_audited(pilot, project, monkeypatch):
    """Every model call is stored with purpose, model and outcome, and linked to the revision it produced."""
    from cadpilot.agents import interpreter, llm
    from cadpilot.agents.interpreter import _LLMInterpretation
    from cadpilot.model.intent import SetGroupCount

    class Msg:
        refusal = None
        parsed = _LLMInterpretation(operations=[SetGroupCount(group="storage", count=4)], clarification=None)

    class Completion:
        model = "vendor/served-model"
        usage = type("U", (), {"prompt_tokens": 1200, "completion_tokens": 40})()
        choices = [type("C", (), {"message": Msg})()]

    class Client:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def parse(**_kw):
                    return Completion

    fake = llm.LLM()
    fake._client = Client()
    fake.model = "requested/model"
    monkeypatch.setattr(interpreter, "get_llm", lambda: fake)
    pid, _ = project
    out = pilot.chat(pid, "please give me four raw milk storage tanks")
    assert out["kind"] == "revision" and out["change_request"]["interpreter"] == "llm"
    calls = pilot.store.llm_calls(pid)
    assert [(c["purpose"], c["outcome"], c["served_model"], c["revision"]) for c in calls] == [
        ("correction_interpreter", "ok", "vendor/served-model", "B")
    ]
    assert calls[0]["model"] == "requested/model" and calls[0]["prompt_tokens"] == 1200
    reply = pilot.store.get(pid).chat[-1]
    assert reply.data["interpreter"] == "llm" and reply.data["llm"][0]["model"] == "vendor/served-model"


def _fake_llm(monkeypatch, module, answer):
    """Point `module`'s get_llm at a model that always returns `answer`."""
    from cadpilot.agents import llm

    class Msg:
        refusal = None
        parsed = answer

    class Completion:
        model = "vendor/served-model"
        usage = None
        choices = [type("C", (), {"message": Msg})()]

    class Client:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def parse(**_kw):
                    return Completion

    fake = llm.LLM()
    fake._client = Client()
    monkeypatch.setattr(module, "get_llm", lambda: fake)
    return fake


def test_wrong_llm_proposal_is_rejected_and_rules_used(pilot, project, monkeypatch):
    """Seen live: the model turned 'Use 4 silos' into 'capacity 4 COUNT'. That must never reach the model."""
    from cadpilot.agents import interpreter
    from cadpilot.agents.interpreter import _LLMInterpretation

    _fake_llm(monkeypatch, interpreter, _LLMInterpretation(
        operations=[SetCapacity(tag="raw_milk_silo", value=4, unit="COUNT")], clarification=None))
    pid, _ = project
    out = pilot.chat(pid, "Use 4 silos")
    assert out["kind"] == "revision"
    assert out["change_request"]["interpreter"].startswith("rules (model proposal rejected")
    silos = [e for e in pilot.store.revision(pid, "B").model.equipment if e.stage == "raw_milk_silo"]
    assert len(silos) == 4 and all(str(e.capacity) == "100 KL" for e in silos)


def test_llm_answers_are_reused_across_regenerations(pilot, monkeypatch):
    """Corrections must not re-ask the model the same question (slow, and could change unrelated equipment)."""
    from cadpilot.agents import process
    from cadpilot.agents.process import _LLMProcessPlan

    _fake_llm(monkeypatch, process, _LLMProcessPlan(exclude_optional_stages=[], constraints=["keep it simple"], assumptions=[]))
    pid = pilot.create_project("memo", demo_data=True).info.id
    pilot.run_generation(pid, "Generate P&ID for milk reception to pasteurization")
    pilot.chat(pid, "Change E-201 to 25 KLPH")
    outcomes = [(c["purpose"], c["outcome"], c["revision"]) for c in pilot.store.llm_calls(pid)]
    assert outcomes == [("process_agent", "ok", "A"), ("process_agent", "cached", "B")]


def test_restore_brings_back_an_earlier_design(pilot, project):
    pid, rev_a = project
    pilot.chat(pid, "Use 4 silos")
    rev_c = pilot.restore(pid, "A")
    assert rev_c.revision == "C" and rev_c.change_request.interpreter == "restore"
    assert {e.tag for e in rev_c.model.equipment} == {e.tag for e in rev_a.model.equipment}
    assert "T-104" in rev_c.diff.equipment.removed
    with pytest.raises(ValueError):
        pilot.restore(pid, "C")


def test_llm_schemas_are_valid_openai_strict_schemas():
    """OpenAI strict structured output rejects `oneOf` (seen live with gpt-4.1-mini: HTTP 400)."""
    import json

    from openai.lib._pydantic import to_strict_json_schema

    from cadpilot.agents.equipment import _LLMEquipmentMapping
    from cadpilot.agents.interpreter import _LLMInterpretation
    from cadpilot.agents.process import _LLMProcessPlan

    for schema in (_LLMInterpretation, _LLMProcessPlan, _LLMEquipmentMapping):
        text = json.dumps(to_strict_json_schema(schema))
        assert '"oneOf"' not in text and '"discriminator"' not in text, schema.__name__
    parsed = _LLMInterpretation.model_validate(
        {"operations": [{"op": "set_group_count", "group": "storage", "count": 4}], "clarification": None}
    )
    assert isinstance(parsed.operations[0], SetGroupCount)


def test_version_history(pilot, project):
    pid, _ = project
    pilot.chat(pid, "Change E-201 to 25 KLPH")
    pilot.approve(pid, "B")
    hist = pilot.store.list_revisions(pid)
    assert [(h.revision, h.status) for h in hist] == [("A", "superseded"), ("B", "approved")]
    assert hist[0].cause.startswith("Generate P&ID") and hist[1].cause == "Change E-201 to 25 KLPH"
    assert hist[1].equipment == 19 and hist[1].approved_by == "reviewer" and "modified" in hist[1].diff_summary


def test_dxf_export(pilot, project, tmp_path):
    import ezdxf

    pid, rev = project
    name, data = pilot.dxf(pid, rev.revision)
    assert name.endswith("_revA.dxf")
    path = tmp_path / name
    path.write_bytes(data)
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    layers = {e.dxf.layer for e in msp}
    assert {"PID-EQUIPMENT", "PID-PIPING", "PID-VALVES", "PID-INSTRUMENTS", "PID-SIGNALS", "PID-HEADERS", "PID-TEXT", "PID-TITLE", "PID-AREAS"} <= layers
    texts = {e.dxf.text for e in msp.query("TEXT")}
    assert {"E-201", "T-101", "HDR-101", "BL-101"} <= texts  # every equipment tag is present as text
    assert any(t.startswith("DN") and "RM-1001" in t for t in texts)  # line numbers
    assert doc.header["$INSUNITS"] == 4
    # coordinates are positive millimetres with Y up
    xs = [v[0] for e in msp.query("LWPOLYLINE") for v in e.get_points("xy")]
    ys = [v[1] for e in msp.query("LWPOLYLINE") for v in e.get_points("xy")]
    assert min(xs) >= 0 and min(ys) >= 0 and max(xs) > 500
