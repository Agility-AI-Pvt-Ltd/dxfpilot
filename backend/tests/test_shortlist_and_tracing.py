"""Equipment-row shortlist for the LLM prompt, and trace naming for the automatic re-read."""

from __future__ import annotations

import os

os.environ["OPENAI_API_KEY"] = ""
os.environ["LANGSMITH_TRACING"] = "false"

from pathlib import Path  # noqa: E402

from cadpilot.agents.equipment import _keyword_match, candidate_rows  # noqa: E402
from cadpilot.ingest.excel import EquipmentRow, load_workbooks  # noqa: E402
from cadpilot.model.engineering import Quantity  # noqa: E402
from cadpilot.standards import get_standards  # noqa: E402

DATA = Path(__file__).parents[2] / "data"
STAGES = [s for s in get_standards().template("milk_reception_to_pasteurization")["stages"] if s["kind"] == "equipment"]


def test_shortlist_is_much_smaller_and_keeps_every_correct_row():
    rows = load_workbooks(sorted(DATA.glob("*.xlsx")), use_llm=False).equipment_rows
    kept, how = candidate_rows(STAGES, rows)
    assert len(kept) < 0.25 * len(rows)
    for s in STAGES:
        assert _keyword_match(s, rows) in kept, s["id"]
        assert how[s["id"]].endswith("by words")


def test_shortlist_keeps_ambiguous_rows_for_the_model_to_decide():
    rows = load_workbooks(sorted(DATA.glob("*.xlsx")), use_llm=False).equipment_rows
    kept, _ = candidate_rows(STAGES, rows)
    pumps = [r.description for r in kept if "pump" in r.description.lower()]
    # several pump rows compete for the two pump stages; the model, not the filter, picks
    assert len(pumps) >= 4 and any("Tanker Unloading Pump" in p for p in pumps)


def test_other_language_falls_back_to_unit_compatible_rows():
    def row(i, desc, value, unit):
        return EquipmentRow(section="", description=desc, capacity=Quantity(value=value, unit=unit), qty=2, ref=f"f.xlsx:S!R{i}")

    rows = [
        row(1, "Pompe de dépotage citerne (variateur)", 40, "KLPH"),
        row(2, "Tank de stockage lait cru", 100, "KL"),
        row(3, "Pasteurisateur à plaques", 20, "KLPH"),
        row(4, "Chariot inox", 600, "kg"),
    ]
    kept, how = candidate_rows(STAGES, rows)
    assert {r.ref for r in kept} >= {"f.xlsx:S!R1", "f.xlsx:S!R2", "f.xlsx:S!R3"}
    assert "unit-compatible" in how["pasteurizer"]
    # a silo stage wants a volume, so it is not offered the flow-rated pumps
    silo_only, _ = candidate_rows([s for s in STAGES if s["id"] == "raw_milk_silo"], rows)
    assert [r.ref for r in silo_only] == ["f.xlsx:S!R2"]


def test_reread_of_old_data_is_one_named_trace(tmp_path):
    from langsmith import Client
    from langsmith.run_helpers import tracing_context

    from cadpilot.service import CadPilot
    from cadpilot.store import FileStore

    runs: dict[str, dict] = {}

    class Capture(Client):
        def create_run(self, name=None, inputs=None, run_type=None, **kw):
            runs[str(kw["id"])] = {"name": name, "parent": str(kw.get("parent_run_id") or "")}

        def update_run(self, run_id, **kw):
            pass

        def batch_ingest_runs(self, create=None, update=None, **kw):
            for c in create or []:
                c = dict(c)
                self.create_run(c.pop("name", None), c.pop("inputs", None), c.pop("run_type", None), **c)

        def multipart_ingest(self, create=None, update=None, **kw):
            self.batch_ingest_runs(create, update)

    pilot = CadPilot(FileStore(tmp_path))
    pid = pilot.create_project("old", demo_data=True).info.id
    old = pilot.store.source_data(pid).model_copy(update={"reader_version": 1})
    pilot.store.save_source_data(pid, old)

    with tracing_context(enabled=True, client=Capture(api_url="http://127.0.0.1:9", api_key="x", auto_batch_tracing=False)):
        data = pilot.source_data(pid)

    assert data.reader_version > 1
    roots = [r["name"] for r in runs.values() if r["parent"] not in runs]
    assert roots == ["cadpilot: re-read workbooks"]
