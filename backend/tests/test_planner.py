"""Experimental LLM planner engine: plan (IR) → deterministic compile → same validator → bounded retry."""

from __future__ import annotations

import os

os.environ["OPENAI_API_KEY"] = ""
os.environ["LANGSMITH_TRACING"] = "false"

import pytest  # noqa: E402

from cadpilot.agents import planner as planner_mod  # noqa: E402
from cadpilot.model.engineering import EngineeringModel  # noqa: E402
from cadpilot.model.ir import (  # noqa: E402
    IRConnection, IRInstrument, IRInstrumentRef, IRLoop, IRNode, IRValve, PIDPlan, PlanPatch, apply_patch,
)
from cadpilot.service import CadPilot  # noqa: E402
from cadpilot.standards import get_standards  # noqa: E402
from cadpilot.store import FileStore  # noqa: E402

REQUEST = "Generate the P&ID for milk reception to pasteurization"


def _owner(m: EngineeringModel, lp) -> str:
    """Equipment a loop belongs to when its measurement sits on a line (the rule's attach side)."""
    meas = m.get_instrument(lp.measured_by)
    rule = next(r for r in get_standards().instrumentation["rules"] if r["id"] == lp.provenance.rule)
    ln = next(x for x in m.lines if x.tag == meas.attached_to.ref)
    return ln.to.item if rule["attach"] == "inlet_line" else ln.from_.item


def ir_from_model(m: EngineeringModel) -> PIDPlan:
    """A correct plan: the rules engine's own draft expressed as IR (what a perfect planner returns)."""
    conn = {ln.tag: f"{ln.from_.item}>{ln.to.item}" for ln in m.lines}
    line_rules = {v["id"] for v in get_standards().line_rules["valves"]}
    inst_rules = {r["id"]: r for r in get_standards().instrumentation["rules"]}
    loop_tags = {lp.tag for lp in m.loops}
    valves = [
        IRValve(on=conn[ln.tag], type=c.type, rule=c.provenance.rule)  # type: ignore[arg-type]
        for ln in m.lines for c in ln.inline if c.provenance.rule in line_rules
    ]
    instruments = []
    for i in m.instruments:
        if i.function.endswith("CV") or i.provenance.agent == "reviewer":
            continue  # created by the loop / by the reviewer, not planned
        if i.tag in loop_tags and not any(lp.measured_by == i.tag for lp in m.loops):
            continue  # a controller: the loop entry creates it
        on = conn[i.attached_to.ref] if i.attached_to.kind == "line" else i.attached_to.ref
        spec = next(s for s in inst_rules[i.provenance.rule]["instruments"] if s["function"] == i.function)
        instruments.append(IRInstrument(function=i.function, on=on, rule=i.provenance.rule, alarms=i.alarms,
                                        inline_element=bool(spec.get("inline"))))
    loops = [IRLoop(rule=lp.provenance.rule, equipment=m.get_instrument(lp.measured_by).attached_to.ref
                    if m.get_instrument(lp.measured_by).attached_to.kind == "equipment"
                    else next(i.provenance for i in m.instruments if i.tag == lp.measured_by) and _owner(m, lp))
             for lp in m.loops]
    return PIDPlan(
        nodes=[IRNode(id=n.tag, kind=n.kind, label=n.label) for n in m.piping_nodes],
        connections=[IRConnection(source=ln.from_.item, target=ln.to.item, kind=ln.kind) for ln in m.lines],
        valves=valves, instruments=instruments, loops=loops, notes=[],
    )


def empty_patch(**kw) -> PlanPatch:
    base = dict(fixes=[], add_nodes=[], remove_nodes=[], add_connections=[], remove_connections=[], add_valves=[],
                remove_valves=[], add_instruments=[], remove_instruments=[], add_loops=[], remove_loops=[])
    return PlanPatch(**(base | kw))


def diff_patch(old: PIDPlan, new: PIDPlan) -> PlanPatch:
    """The patch a perfect planner would return to turn `old` into `new`."""
    def minus(a, b):
        return [x for x in a if x not in b]
    return empty_patch(
        add_nodes=minus(new.nodes, old.nodes), remove_nodes=[n.id for n in minus(old.nodes, new.nodes)],
        add_connections=minus(new.connections, old.connections),
        remove_connections=[f"{c.source}>{c.target}" for c in minus(old.connections, new.connections)],
        add_valves=minus(new.valves, old.valves), remove_valves=minus(old.valves, new.valves),
        add_instruments=minus(new.instruments, old.instruments),
        remove_instruments=[IRInstrumentRef(function=i.function, on=i.on) for i in minus(old.instruments, new.instruments)],
        add_loops=minus(new.loops, old.loops),
        remove_loops=minus(old.loops, new.loops),
    )


class FakePlanner:
    """Stands in for the model: the first plan, then scripted correction patches; records every prompt."""

    enabled = True
    model = "fake/planner"

    def __init__(self, plan: PIDPlan, patches=()):
        self.plan = plan
        self.patches = list(patches)
        self.prompts: list[str] = []

    def structured(self, system, prompt, schema, purpose, **_kw):
        self.prompts.append(prompt)
        if schema is PlanPatch:
            return self.patches.pop(0) if self.patches else empty_patch()
        return section_of(self.plan, prompt)


def section_of(plan: PIDPlan, prompt: str) -> PIDPlan:
    """What a planner asked for one section returns: the part of the plan for the equipment in its prompt."""
    listing = prompt.split("Equipment (use these tags):\n", 1)[1].split("\n\n", 1)[0]
    tags = {line[2:].split(" | ")[0] for line in listing.splitlines() if line.startswith("- ")}
    keep = set(tags)
    for _ in range(3):  # headers and terminals reached through the section's equipment
        for c in plan.connections:
            if c.source in keep or c.target in keep:
                keep |= {c.source, c.target}
    conns = [c for c in plan.connections if c.source in keep and c.target in keep]
    names = {f"{c.source}>{c.target}" for c in conns}
    return PIDPlan(
        nodes=[n for n in plan.nodes if n.id in keep or not any(n.id in (c.source, c.target) for c in plan.connections)],
        connections=conns,
        valves=[v for v in plan.valves if v.on in names],
        instruments=[i for i in plan.instruments if i.on in names or i.on in tags],
        loops=[lp for lp in plan.loops if lp.equipment in tags], notes=[],
    )


@pytest.fixture()
def pilot(tmp_path):
    return CadPilot(FileStore(tmp_path))


@pytest.fixture()
def reference(pilot) -> EngineeringModel:
    pid = pilot.create_project("rules reference", demo_data=True).info.id
    return pilot.run_generation(pid, REQUEST).model


def planner_project(pilot, monkeypatch, plan, patches=()) -> tuple[str, FakePlanner]:
    fake = FakePlanner(plan, patches)
    monkeypatch.setattr(planner_mod, "get_llm", lambda: fake)
    pid = pilot.create_project("planner", demo_data=True).info.id
    return pid, fake


def test_correct_plan_passes_first_time_and_matches_the_rules_draft(pilot, reference, monkeypatch):
    pid, fake = planner_project(pilot, monkeypatch, ir_from_model(reference))
    rev = pilot.run_generation(pid, REQUEST, engine="llm_planner")
    assert rev.validation.passed, [i.message for i in rev.validation.errors]
    assert rev.model.metadata["engine"] == "llm_planner" and rev.model.metadata["planner"]["attempts"] == 1
    assert rev.model.counts() == reference.counts()
    assert {e.tag for e in rev.model.equipment} == {e.tag for e in reference.equipment}
    assert len(fake.prompts) == 1 and "LINE RULES (read-only)" in fake.prompts[0]


def test_failed_plan_gets_the_errors_back_and_is_corrected(pilot, reference, monkeypatch):
    good = ir_from_model(reference)
    broken = good.model_copy(deep=True)
    broken.valves = [v for v in broken.valves if v.rule != "LR-PUMP-SUCTION"]
    broken.instruments = [i for i in broken.instruments if i.function != "PDI"]
    pid, fake = planner_project(pilot, monkeypatch, broken, [diff_patch(broken, good)])
    rev = pilot.run_generation(pid, REQUEST, engine="llm_planner")

    assert rev.validation.passed
    assert len(fake.prompts) == 2
    feedback = fake.prompts[1]
    assert "FAILED VALIDATION" in feedback and "PLAN TO CORRECT (attempt 1" in feedback and "PATCH" in feedback
    assert "[MISSING_VALVE] [rule LR-PUMP-SUCTION]" in feedback and "[MISSING_INSTRUMENT] [rule IR-STRAINER-DP]" in feedback
    assert "(= connection " in feedback  # line tags are translated back into the planner's own connections
    # each missing item says where it is expected, in the planner's own ids
    assert "expected: butterfly_valve on V-101>P-101 (rule LR-PUMP-SUCTION)" in feedback
    assert "expected: instrument PDI on F-101 (rule IR-STRAINER-DP)" in feedback
    history = rev.model.metadata["planner"]["history"]
    assert [h["attempt"] for h in history] == [1, 2] and history[0]["errors"] and not history[1]["errors"]
    assert history[1]["changes"].startswith("+ ") and history[1]["kept"]
    summaries = [d["summary"] for d in rev.model.metadata["decisions"]]
    assert any("attempt 1 rejected" in s for s in summaries) and any("accepted on attempt 2" in s for s in summaries)


def test_stops_early_when_corrections_do_not_help(pilot, reference, monkeypatch):
    broken = ir_from_model(reference)
    # a structural break: which unit feeds which is the planner's decision, never completed for it
    broken.connections = broken.connections[:-1]
    pid, fake = planner_project(pilot, monkeypatch, broken)  # every correction is an empty patch
    rev = pilot.run_generation(pid, REQUEST, engine="llm_planner")

    assert 1 + planner_mod.NO_PROGRESS_LIMIT <= len(fake.prompts) < planner_mod.MAX_ATTEMPTS
    assert "(STILL UNFIXED" not in fake.prompts[1]  # first correction: nothing has been tried yet
    assert "STILL UNFIXED — also reported after attempt 2" in fake.prompts[2]
    # before giving up it plans the stuck section again from scratch, as often as it is allowed to
    replans = [p for p in fake.prompts if "COULD NOT BE FIXED BY PATCHING" in p]
    assert len(replans) == planner_mod.SECTION_REPLANS and "Errors it still had:" in replans[0]
    meta = rev.model.metadata["planner"]
    assert meta["best_attempt"] == 1 and "no improvement" in meta["stop_reason"]
    assert rev.status == "needs_attention" and not rev.validation.passed
    assert len(rev.validation.errors) == len(meta["history"][0]["errors"])  # the best attempt is what the reviewer gets
    assert any("human review" in d["summary"] for d in rev.model.metadata["decisions"])
    with pytest.raises(ValueError):
        pilot.approve(pid, rev.revision)


def test_steady_progress_runs_to_the_attempt_limit(pilot, reference, monkeypatch):
    good = ir_from_model(reference)
    broken = good.model_copy(deep=True)
    broken.instruments = []  # fixed one instrument per correction: always progress, never done
    patches = [empty_patch(add_instruments=[i]) for i in good.instruments]
    pid, fake = planner_project(pilot, monkeypatch, broken, patches)
    rev = pilot.run_generation(pid, REQUEST, engine="llm_planner")
    assert len(fake.prompts) == planner_mod.MAX_ATTEMPTS
    meta = rev.model.metadata["planner"]
    assert meta["best_attempt"] == planner_mod.MAX_ATTEMPTS and "limit" in meta["stop_reason"]


def test_a_worse_correction_is_discarded_and_the_best_plan_is_kept(pilot, reference, monkeypatch):
    good = ir_from_model(reference)
    broken = good.model_copy(deep=True)
    broken.valves = [v for v in broken.valves if v.rule != "LR-PUMP-SUCTION"]
    worse = empty_patch(remove_instruments=[IRInstrumentRef(function=i.function, on=i.on) for i in good.instruments[:5]])
    pid, fake = planner_project(pilot, monkeypatch, broken, [worse, diff_patch(broken, good)])
    rev = pilot.run_generation(pid, REQUEST, engine="llm_planner")

    history = rev.model.metadata["planner"]["history"]
    assert [h["kept"] for h in history] == [True, False, True]
    assert "did not improve the plan" in fake.prompts[2] and "PLAN TO CORRECT (attempt 1" in fake.prompts[2]
    assert rev.validation.passed and rev.model.counts() == reference.counts()


def test_rule_items_the_planner_never_fixes_are_completed_by_the_system(pilot, reference, monkeypatch):
    """A missing valve, instrument or loop has one right answer, so the loop does not end on one."""
    good = ir_from_model(reference)
    broken = good.model_copy(deep=True)
    broken.loops = []
    broken.valves = [v for v in broken.valves if v.rule != "LR-PUMP-SUCTION"]
    worse = empty_patch(remove_instruments=[IRInstrumentRef(function=i.function, on=i.on) for i in good.instruments[:5]])
    pid, _ = planner_project(pilot, monkeypatch, broken, [worse, worse])  # the planner never fixes anything
    rev = pilot.run_generation(pid, REQUEST, engine="llm_planner")

    meta = rev.model.metadata["planner"]
    assert rev.validation.passed and rev.status == "ready_for_review", [i.message for i in rev.validation.errors]
    assert meta["best_attempt"] == 1 and meta["attempts"] < planner_mod.MAX_ATTEMPTS
    assert "no improvement" in meta["stop_reason"] and "completed by the system" in meta["stop_reason"]
    completed = meta["auto_completed"]
    assert len(completed) == len(good.loops) + len([v for v in good.valves if v.rule == "LR-PUMP-SUCTION"])
    assert any("LIC loop (IR-" in c for c in completed) and any("butterfly_valve (LR-PUMP-SUCTION)" in c for c in completed)
    assert rev.model.counts()["control_loops"] == reference.counts()["control_loops"]
    assert any("completed" in d["summary"] for d in rev.model.metadata["decisions"])
    pilot.approve(pid, rev.revision)  # a complete draft can be approved


def test_compiler_rejects_duplicates_and_rules_used_in_the_wrong_place(pilot, reference, monkeypatch):
    plan = ir_from_model(reference)
    suction = next(v for v in plan.valves if v.rule == "LR-PUMP-SUCTION")
    discharge_line = next(v.on for v in plan.valves if v.rule == "LR-PUMP-DISCHARGE-NRV")
    plan.valves += [suction, IRValve(on=discharge_line, type="butterfly_valve", rule="LR-PUMP-SUCTION")]
    pid, fake = planner_project(pilot, monkeypatch, plan, [empty_patch(remove_valves=[suction.model_copy(update={"on": "X>Y"})])])
    pilot.run_generation(pid, REQUEST, engine="llm_planner")
    feedback = fake.prompts[1]
    assert "[IR_DUPLICATE_ENTRY]" in feedback and "[IR_RULE_NOT_APPLICABLE]" in feedback
    assert "remove_valves: no valve X>Y" in fake.prompts[2]  # a patch that matches nothing is reported back


def test_feedback_names_nodes_by_the_planners_own_ids(pilot, reference, monkeypatch):
    plan = ir_from_model(reference)
    plan.nodes.append(IRNode(id="spare_header", kind="header", label=""))
    pid, fake = planner_project(pilot, monkeypatch, plan)
    pilot.run_generation(pid, REQUEST, engine="llm_planner")
    feedback = fake.prompts[1]
    assert "[ORPHAN]" in feedback and "node 'spare_header'" in feedback
    assert "HDR-" not in feedback.split("FAILED VALIDATION")[1].split("PATCH")[0]


def test_prompt_lists_where_each_instrumentation_rule_applies(pilot, reference, monkeypatch):
    pid, fake = planner_project(pilot, monkeypatch, ir_from_model(reference))
    pilot.run_generation(pid, REQUEST, engine="llm_planner")
    strainers = sorted(e.tag for e in reference.equipment if e.type == "duplex_strainer")
    assert f"required for EVERY one of: {', '.join(strainers)}" in fake.prompts[0]


def test_apply_patch_cascades_and_reports_misses():
    plan = PIDPlan(nodes=[IRNode(id="h", kind="header", label="")],
                   connections=[IRConnection(source="A", target="h", kind="process"), IRConnection(source="h", target="B", kind="process")],
                   valves=[IRValve(on="A>h", type="on_off_valve", rule="LR-MANIFOLD-ROUTE")],
                   instruments=[IRInstrument(function="PI", on="h>B", rule="R", alarms=[], inline_element=False)], loops=[], notes=[])
    new, problems, summary = apply_patch(plan, empty_patch(remove_nodes=["h"], remove_connections=["Q>R"]))
    assert not new.nodes and not new.connections and not new.valves and not new.instruments
    assert problems == ["remove_connections: no connection 'Q>R' in the plan"] and "− 1 nodes" in summary


def test_planner_cannot_invent_rules_or_waive_checks(pilot, reference, monkeypatch):
    sneaky = ir_from_model(reference)
    sneaky.valves = [v.model_copy(update={"rule": "LR-ANYTHING-GOES"}) if v.rule == "LR-PUMP-SUCTION" else v for v in sneaky.valves]
    pid, _ = planner_project(pilot, monkeypatch, sneaky)
    rev = pilot.run_generation(pid, REQUEST, engine="llm_planner")
    codes = {i.code for i in rev.validation.errors}
    assert "IR_UNKNOWN_RULE" in codes  # the invented rule is rejected, never applied
    # and the rule it tried to dodge is still enforced: the system puts the real valve in itself
    assert any("LR-PUMP-SUCTION" in c for c in rev.model.metadata["planner"]["auto_completed"])
    assert any(c.provenance.rule == "LR-PUMP-SUCTION" for ln in rev.model.lines for c in ln.inline)
    # the IR has nowhere to put a rule, a waiver, a tag or a size
    fields = set(PIDPlan.model_fields) | {f for m in (IRValve, IRInstrument, IRLoop, IRConnection, IRNode) for f in m.model_fields}
    assert not fields & {"waiver", "waivers", "suppressed", "severity", "tag", "size_dn", "design_flow", "rules"}


def test_no_model_falls_back_to_the_rules_engine(pilot, reference):
    pid = pilot.create_project("no model", demo_data=True).info.id
    rev = pilot.run_generation(pid, REQUEST, engine="llm_planner")  # OPENAI_API_KEY is empty in tests
    assert rev.validation.passed and rev.model.metadata["engine"] == "rules"
    assert any("LLM planner unavailable" in d["summary"] for d in rev.model.metadata["decisions"])
    assert rev.model.counts() == reference.counts()


def test_corrections_keep_the_planner_engine(pilot, reference, monkeypatch):
    pid, fake = planner_project(pilot, monkeypatch, ir_from_model(reference))
    pilot.run_generation(pid, REQUEST, engine="llm_planner")
    out = pilot.chat(pid, "Change E-201 to 25 KLPH")
    rev = pilot.store.revision(pid, out["revision"])
    assert rev.model.metadata["engine"] == "llm_planner" and len(fake.prompts) == 2
    assert str(rev.model.get_equipment("E-201").capacity) == "25 KLPH"


def test_loops_are_derived_from_the_rule_and_mistakes_are_explained(pilot, reference, monkeypatch):
    plan = ir_from_model(reference)
    assert all(set(IRLoop.model_fields) == {"rule", "equipment"} for _ in [0])  # the planner only says which loop exists
    chiller = next(lp for lp in plan.loops if lp.rule == "IR-CHILLER-TEMP")
    tt = next(i for i in plan.instruments if i.rule == "IR-CHILLER-TEMP")
    plan.instruments = [i for i in plan.instruments if i is not tt] + [
        IRInstrument(function="TIC", on=chiller.equipment, rule="IR-CHILLER-TEMP", alarms=[], inline_element=False),  # a controller
        IRInstrument(function="TT", on=chiller.equipment, rule="IR-CHILLER-TEMP", alarms=[], inline_element=False),  # wrong side
    ]
    plan.loops.append(chiller)  # listed twice
    pid, fake = planner_project(pilot, monkeypatch, plan)
    pilot.run_generation(pid, REQUEST, engine="llm_planner")
    feedback = fake.prompts[1]
    assert "is the controller of a loop, not an instrument" in feedback
    assert "puts it on the connection out of" in feedback
    assert f"Loop IR-CHILLER-TEMP for {chiller.equipment} is listed twice" in feedback
    assert f"plan the measuring instrument TT for {chiller.equipment}" in feedback


def test_topology_errors_come_first_with_the_expected_connection(pilot, reference, monkeypatch):
    plan = ir_from_model(reference)
    cut = next(c for c in plan.connections if c.source == "S-201")  # separator → pasteurizer
    plan.connections = [c for c in plan.connections if c is not cut]
    plan.valves = [v for v in plan.valves if v.on != f"{cut.source}>{cut.target}"]
    pid, fake = planner_project(pilot, monkeypatch, plan)
    pilot.run_generation(pid, REQUEST, engine="llm_planner")
    errors = [x for x in fake.prompts[1].split("every one must be fixed:\n")[1].split("\n\n")[0].splitlines() if x[:1].isdigit()]
    codes = [line.split("]")[0].split("[")[1] for line in errors]
    first_rule_item = min(k for k, c in enumerate(codes) if c.startswith("MISSING_"))
    assert all(not c.startswith("MISSING_") for c in codes[:first_rule_item]) and "NO_OUTLET" in codes[:first_rule_item]
    assert any("NO_OUTLET" in line and "expected (flow order of the stages): connection S-201>E-201" in line for line in errors)


def test_removing_a_duplicate_keeps_the_other_copy_and_adding_twice_is_reported():
    loop = IRLoop(rule="IR-CHILLER-TEMP", equipment="E-101")
    plan = PIDPlan(nodes=[], connections=[], valves=[], instruments=[], loops=[loop, loop], notes=[])
    new, problems, _ = apply_patch(plan, empty_patch(remove_loops=[loop]))
    assert new.loops == [loop] and not problems
    again, problems, _ = apply_patch(new, empty_patch(add_loops=[loop]))
    assert again.loops == [loop] and "already in the plan" in problems[0]


def test_silos_wired_straight_to_pumps_get_a_concrete_header_recipe(pilot, reference, monkeypatch):
    """The trace case: 3 silos wired 1:1 to 2 pumps, reusing the inlet header for T-103's outlet."""
    plan = ir_from_model(reference)
    hdr_in = next(c.source for c in plan.connections if c.target == "T-101")
    hdr_out = next(c.target for c in plan.connections if c.source == "T-101")
    plan.nodes = [n for n in plan.nodes if n.id != hdr_out]
    plan.connections = [c for c in plan.connections if hdr_out not in (c.source, c.target)] + [
        IRConnection(source="T-101", target="P-201", kind="process"),
        IRConnection(source="T-102", target="P-202", kind="process"),
    ]
    plan.valves = [v for v in plan.valves if hdr_out not in v.on]
    pid, fake = planner_project(pilot, monkeypatch, plan)
    pilot.run_generation(pid, REQUEST, engine="llm_planner")
    feedback = fake.prompts[1]
    assert "[IR_GROUP_ROUTING]" in feedback and "Connection T-101>P-201 links 3 unit(s)" in feedback
    assert "a NEW header between T-101, T-102, T-103 and P-201, P-202" in feedback
    assert f"do not reuse {hdr_in}" in feedback and "remove the direct connections T-101>P-201" in feedback


def test_cycle_is_located_in_the_planners_names(pilot, reference, monkeypatch):
    plan = ir_from_model(reference)
    hdr_in = next(c.source for c in plan.connections if c.target == "T-103")
    plan.connections = [c for c in plan.connections if c.source != "T-103"] + [IRConnection(source="T-103", target=hdr_in, kind="process")]
    pid, fake = planner_project(pilot, monkeypatch, plan)
    pilot.run_generation(pid, REQUEST, engine="llm_planner")
    feedback = fake.prompts[1]
    assert "[CYCLE]" in feedback and f"the loop is {hdr_in} > T-103 > {hdr_in}" in feedback


@pytest.mark.parametrize("modules", [["cip"], ["steam"], ["reception", "pasteurization"]])
def test_planner_ir_works_for_engineering_modules(pilot, monkeypatch, modules):
    """A correct plan of a module drawing (side feeds, aux ports, utility outlets) compiles and validates."""
    ref_pid = pilot.create_project("module reference", demo_data=True).info.id
    reference = pilot.run_generation(ref_pid, REQUEST, modules=modules).model
    pid, fake = planner_project(pilot, monkeypatch, ir_from_model(reference))
    rev = pilot.run_generation(pid, REQUEST, engine="llm_planner", modules=modules)
    assert rev.validation.passed, [i.message for i in rev.validation.errors]
    assert rev.model.metadata["engine"] == "llm_planner" and rev.model.counts() == reference.counts()
    assert "module " in fake.prompts[0]


def test_sections_are_planned_separately_and_only_failing_ones_are_corrected(pilot, monkeypatch):
    modules = ["reception", "cip"]
    ref_pid = pilot.create_project("sections reference", demo_data=True).info.id
    reference = pilot.run_generation(ref_pid, REQUEST, modules=modules).model
    good = ir_from_model(reference)
    cip_tags = {e.tag for e in reference.equipment if e.area == "3"}
    broken = good.model_copy(deep=True)
    broken.loops = [lp for lp in broken.loops if lp.equipment not in cip_tags]  # only the CIP section is wrong
    fix = empty_patch(add_loops=[lp for lp in good.loops if lp.equipment in cip_tags])
    pid, fake = planner_project(pilot, monkeypatch, broken, [fix])
    rev = pilot.run_generation(pid, REQUEST, engine="llm_planner", modules=modules)

    assert rev.validation.passed, [i.message for i in rev.validation.errors]
    plans, corrections = fake.prompts[:2], fake.prompts[2:]
    assert len(plans) == 2 and len(corrections) == 1  # one plan per section, one correction for CIP only
    assert all("CONNECTION PLAN" in p and "REQUIRED ITEMS PER EQUIPMENT" in p for p in plans)
    assert "T-301" in corrections[0] and "T-101" not in corrections[0].split("PLAN TO CORRECT")[0]
    # node ids never clash between sections
    ids = [n["id"] for n in rev.model.metadata["planner"]["final_plan"]["nodes"]]
    assert len(ids) == len(set(ids))


def test_section_node_ids_are_prefixed_but_equipment_tags_never_renamed():
    p = PIDPlan(nodes=[IRNode(id="hdr_1", kind="header", label=""), IRNode(id="V-101", kind="header", label="")],
                connections=[IRConnection(source="E-101", target="hdr_1", kind="process"),
                             IRConnection(source="hdr_1", target="V-101", kind="process")],
                valves=[IRValve(on="E-101>hdr_1", type="on_off_valve", rule="LR-MANIFOLD-ROUTE")],
                instruments=[], loops=[], notes=[])
    q = planner_mod.prefix_nodes(p, "reception/")
    assert [n.id for n in q.nodes] == ["reception/hdr_1", "V-101"]  # a tag-like id stays: the compiler reports it
    assert q.connections[1].source == "reception/hdr_1" and q.valves[0].on == "E-101>reception/hdr_1"


def test_checklist_states_where_every_item_goes(pilot, monkeypatch):
    pid, fake = planner_project(pilot, monkeypatch, PIDPlan(nodes=[], connections=[], valves=[], instruments=[], loops=[], notes=[]))
    pilot.run_generation(pid, REQUEST, engine="llm_planner", modules=["reception"])
    prompt = fake.prompts[0]
    assert "through ONE header of their own: each of the 2 → header → each of the 3" in prompt  # chillers → silos
    assert "instrument TT alarms ['TAH'] on the connection OUT OF E-101 ('E-101>' + your header id) rule IR-CHILLER-TEMP" in prompt
    assert "valve check_valve rule LR-PUMP-DISCHARGE-NRV on the connection OUT OF P-101 ('P-101>E-101')" in prompt
    assert "loop {rule: IR-CHILLER-TEMP, equipment: E-101}" in prompt
    assert "loop {rule: IR-PUMP-DISCHARGE-PRESSURE" not in prompt  # no loop entry for a rule without a loop
    assert "LR-MANIFOLD-ROUTE on every one of the connections with a header" in prompt
