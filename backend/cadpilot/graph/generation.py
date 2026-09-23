"""Autonomous generation workflow (LangGraph).

START → plan → {process ∥ equipment} → reconcile ─┬─ rules engine:  topology → instrumentation → merge ─┐
                                                  └─ LLM planner:   llm_planner → compile_ir ───────────┤
      validate ─┬─ rules:   (replan → worker … | finalize)                                               ◄┘
                └─ planner: (fail and attempts < MAX_ATTEMPTS → llm_planner with the errors | finalize) → layout → render → END

The LLM planner (experimental, DesignIntent.engine = "llm_planner") proposes a P&ID IR that is compiled
deterministically and checked by the same validator; once the attempts run out the draft is committed as
needs_attention for human review. If the model is unavailable the rules engine takes over.

Process and Equipment work in parallel; Topology needs both (it connects the equipment in the
process order) and Instrumentation needs the topology (it attaches to lines).
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..agents import equipment as equipment_agent
from ..agents import instrumentation as instrumentation_agent
from ..agents import planner as planner_agent
from ..agents import primary
from ..agents import process as process_agent
from ..agents import topology as topology_agent
from ..agents.context import AgentContext
from ..engine.ir_compile import compile_plan
from ..engine.layout import layout
from ..engine.merge import assemble, reconcile
from ..engine.render import render_svg
from ..engine.tags import TagRegistry
from ..engine.validate import validate
from ..ingest.excel import SourceData
from ..model.engineering import Decision, EngineeringModel, Equipment, ProcessDefinition, ProjectInfo
from ..model.intent import DesignIntent
from ..model.proposals import (
    EquipmentProposal,
    InstrumentationProposal,
    ProcessProposal,
    TopologyProposal,
    ValidationIssue,
    ValidationReport,
)
from ..standards import Standards, get_standards


class GenerationState(TypedDict, total=False):
    project: ProjectInfo
    source: SourceData
    intent: DesignIntent
    registry: TagRegistry
    revision: str

    plan: dict[str, Any]
    process_proposal: ProcessProposal
    equipment_proposal: EquipmentProposal
    process: ProcessDefinition
    equipment: list[Equipment]
    topology_proposal: TopologyProposal
    instrumentation_proposal: InstrumentationProposal
    merged_model: EngineeringModel
    validation: ValidationReport
    feedback: list[ValidationIssue]
    iteration: int
    replan_target: str | None
    svg: str
    status: str

    # experimental LLM planner engine
    engine: str  # "rules" | "llm_planner" — what actually produced this draft
    ir: Any  # PIDPlan of the current attempt
    ir_attempt: int
    ir_errors: list[ValidationIssue]  # compiler + validator errors of the current attempt
    ir_warnings: list[ValidationIssue]
    connection_of_line: dict[str, str]
    ir_history: list[dict[str, Any]]
    ir_hints: dict[int, str]
    node_of_tag: dict[str, str]
    ir_best: dict[str, Any]  # the attempt with the fewest errors: plan, model, report, feedback inputs
    ir_seen: dict[str, list[int]]  # error signature -> attempts it was reported after
    ir_changes: str  # what the last correction patch changed
    ir_patch_problems: list[str]
    ir_sections: dict[str, Any]  # section (module) -> its current plan
    ir_section_best: dict[str, dict[str, Any]]  # section -> best plan + the errors / hints it was judged by
    ir_section_counts: dict[str, int]  # section -> errors in the latest attempt
    ir_patch_problems_by: dict[str, list[str]]  # section -> removals of its last patch that matched nothing
    ir_final: str  # set when the best version of every section is compiled once more before review
    ir_repair: dict  # the rule items the closer completed after the planner stopped
    ir_replanned: list[str]  # sections planned again from scratch on this attempt
    ir_replans: dict  # section -> how many times it has been planned again
    ir_last_replan: int  # attempt of the most recent fresh plan (it gets a few corrections before stopping)
    next_after_validate: str

    decisions: Annotated[list[Decision], operator.add]
    events: Annotated[list[dict[str, Any]], operator.add]


def _ctx(state: GenerationState, standards: Standards) -> AgentContext:
    return AgentContext(
        standards=standards,
        source=state["source"],
        intent=state["intent"],
        registry=state["registry"],
        feedback=state.get("feedback", []),
    )


REPAIRED = "repair|"  # ir_final marker: the plan the closer completed, on its way back through compile_ir


def _event(step: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"step": step, "detail": detail, **extra}


def build_generation_graph(standards: Standards | None = None):
    std = standards or get_standards()

    def plan(state: GenerationState) -> dict:
        intent = state["intent"]
        template, decision = primary.choose_template(intent.request, std, intent.template)
        intent = intent.model_copy(update={"template": template})
        iteration = state.get("iteration", 0)
        src = state["source"]
        detail = (
            f"{len(src.equipment_rows)} design-data rows, "
            f"{len(src.mass_balance_inputs)} mass-balance input(s); delegating to 4 workers"
        )
        if iteration:
            detail = f"Replan #{iteration}: re-running process and equipment analysis"
        return {
            "intent": intent,
            "plan": {"template": template, "workers": primary.WORKER_ORDER},
            "decisions": [] if iteration else [decision],
            "events": [_event("plan", detail)],
        }

    def process_node(state: GenerationState) -> dict:
        p = process_agent.run(_ctx(state, std))
        return {
            "process_proposal": p,
            "events": [_event("process_agent", f"Process requirements identified — {len(p.process.stages)} stages", llm=p.used_llm)],
        }

    def equipment_node(state: GenerationState) -> dict:
        p = equipment_agent.run(_ctx(state, std))
        return {
            "equipment_proposal": p,
            "events": [_event("equipment_agent", f"Equipment identified — {len(p.equipment)} candidate items from {len(p.source_rows)} data rows", llm=p.used_llm)],
        }

    def reconcile_node(state: GenerationState) -> dict:
        proc, eqs, decisions = reconcile(state["process_proposal"], state["equipment_proposal"], state["intent"])
        return {
            "process": proc,
            "equipment": eqs,
            "decisions": decisions,
            "events": [_event("reconcile", f"Primary agent reconciled proposals — {len(eqs)} equipment in scope")],
        }

    def topology_node(state: GenerationState) -> dict:
        p = topology_agent.run(_ctx(state, std), state["process"], state["equipment"])
        return {
            "topology_proposal": p,
            "events": [_event("topology_agent", f"Process topology generated — {len(p.lines)} lines, {len(p.piping_nodes)} piping nodes")],
        }

    def instrumentation_node(state: GenerationState) -> dict:
        t = state["topology_proposal"]
        p = instrumentation_agent.run(_ctx(state, std), state["equipment"], t.piping_nodes, t.lines)
        return {
            "instrumentation_proposal": p,
            "events": [_event("instrumentation_agent", f"Instrumentation generated — {len(p.instruments)} instruments, {len(p.loops)} loops")],
        }

    def _assemble(state: GenerationState, topo: TopologyProposal, instr: InstrumentationProposal) -> EngineeringModel:
        model = assemble(
            state["project"], state["process"], state["process_proposal"], state["equipment"],
            topo, instr, state["intent"], state.get("decisions", []),
        )
        model.metadata["warnings"] = [
            w for p in (state["process_proposal"], state["equipment_proposal"], topo, instr) for w in p.warnings
        ]
        model.metadata["assumptions"] = state["process_proposal"].assumptions + state["equipment_proposal"].assumptions
        return model

    def merge_node(state: GenerationState) -> dict:
        model = _assemble(state, state["topology_proposal"], state["instrumentation_proposal"])
        model.metadata["engine"] = "rules"
        return {"merged_model": model, "engine": "rules", "events": [_event("merge", "Proposals merged into the engineering model")]}

    # ---- experimental LLM planner: plan (IR) → compile → same validator → retry with errors -----
    def route_after_reconcile(state: GenerationState) -> str:
        return "llm_planner" if state["intent"].engine == "llm_planner" else "topology_agent"

    def llm_planner_node(state: GenerationState) -> dict:
        """Plan (attempt 1) or correct (later attempts), one engineering section at a time, in parallel."""
        attempt = state.get("ir_attempt", 0) + 1
        ctx = _ctx(state, std)
        process, equipment = state["process"], state["equipment"]
        sections = planner_agent.module_keys(process)

        def job(m: str, fn):
            proc_m, eq_m = planner_agent.scope(process, equipment, m)
            return lambda: fn(proc_m, eq_m, planner_agent.section_prefix(m, process))

        if attempt == 1:
            plans = planner_agent.in_parallel({
                m: job(m, lambda p, e, pre: planner_agent.plan(ctx, p, e, equipment, pre)) for m in sections
            })
            if all(pl is None for pl in plans.values()):
                return {
                    "ir": None, "ir_attempt": attempt,
                    "decisions": [Decision(agent=primary.AGENT, summary="LLM planner unavailable — rules engine used",
                                           detail="No usable plan from the model (not configured, timed out or invalid reply)")],
                    "events": [_event("llm_planner", f"Attempt {attempt}: no usable plan — falling back to the rules engine")],
                }
            missing = [m or "plan" for m, pl in plans.items() if pl is None]
            current = {m: pl or planner_agent.EMPTY_PLAN for m, pl in plans.items()}
            ir = planner_agent.merge_plans(current)
            where = f" in {len(sections)} sections (parallel)" if len(sections) > 1 else ""
            return {"ir": ir, "ir_sections": current, "ir_attempt": attempt, "ir_changes": "first plan", "ir_patch_problems": [],
                    "events": [_event("llm_planner", f"Attempt {attempt}: LLM proposed {planner_agent.summarize(ir)}{where}"
                                      + (f"; no answer for {', '.join(missing)}" if missing else ""), llm=True)]}

        best_sec = state["ir_section_best"]
        counts = state.get("ir_section_counts", {})
        to_fix = [m for m in sections if best_sec[m]["errors"]]
        problems_by = state.get("ir_patch_problems_by", {}) if isinstance(state.get("ir_patch_problems_by"), dict) else {}

        def replan(m: str, b: dict):
            """A section stuck for several corrections: plan it again, told what it kept getting wrong."""
            problems = planner_agent.format_feedback(b["errors"], b["col"], b["hints"], b["nodes"])
            return job(m, lambda p, e, pre: planner_agent.plan(ctx, p, e, equipment, pre, attempt, b["plan"], problems))

        def correct(m: str):
            b = best_sec[m]
            rejected = None
            if counts.get(m, 0) > b["count"] and b["attempt"] != attempt - 1:
                rejected = (f"Your last patch (attempt {attempt - 1}) did not improve the plan — {counts[m]} error(s) against "
                            f"{b['count']} — and was discarded. Correct the best plan instead.")
            return job(m, lambda p, e, pre: planner_agent.correct(
                ctx, p, e, attempt, b["plan"], b["attempt"], b["errors"], b["warnings"], b["col"], b["nodes"], b["hints"],
                state.get("ir_seen", {}), problems_by.get(m, state.get("ir_patch_problems", []) if len(sections) == 1 else []),
                rejected, equipment, pre,
            ))

        replans = dict(state.get("ir_replans", {}))
        stuck = [m for m in to_fix if attempt - best_sec[m]["attempt"] >= planner_agent.SECTION_REPLAN_AFTER
                 and replans.get(m, 0) < planner_agent.SECTION_REPLANS]
        jobs = {m: (replan(m, best_sec[m]) if m in stuck else correct(m)) for m in to_fix}
        results = planner_agent.in_parallel(jobs)
        fresh = {m: results.pop(m) for m in list(results) if m in stuck}  # a fresh plan, not a patch
        if to_fix and not fresh and all(out is None for out in results.values()):  # keep what we have: the best plan goes to review
            return {
                "ir": None, "ir_attempt": attempt, **_restore_best(state, f"no usable correction from the model on attempt {attempt}"),
                "events": [_event("llm_planner", f"Attempt {attempt}: no usable correction — keeping the best plan")],
            }
        current, changes, problems = {}, [], {}
        replanned = set()
        for m in sections:
            if m in stuck:
                pl = fresh.get(m)
                current[m] = pl if pl is not None else best_sec[m]["plan"]
                if pl is not None:
                    replanned.add(m)
                    replans[m] = replans.get(m, 0) + 1
                    changes.append(f"{m}: planned again from scratch ({planner_agent.summarize(pl)})")
                continue
            out = results.get(m)
            if out is None:
                current[m] = best_sec[m]["plan"]
                continue
            current[m], _patch, problems[m], ch = out
            changes.append(f"{m}: {ch}" if len(sections) > 1 else ch)
        ir = planner_agent.merge_plans(current)
        base_attempts = sorted({best_sec[m]["attempt"] for m in to_fix})
        label = (f"patched {len(to_fix)} of {len(sections)} sections" if len(sections) > 1
                 else f"patched attempt {base_attempts[0]}")
        return {
            "ir": ir, "ir_sections": current, "ir_attempt": attempt, "ir_replanned": sorted(replanned), "ir_replans": replans,
            "ir_last_replan": attempt if replanned else state.get("ir_last_replan", 0),
            "ir_changes": "; ".join(changes) or "no changes",
            "ir_patch_problems": [x for v in problems.values() for x in v], "ir_patch_problems_by": problems,
            "events": [_event("llm_planner", f"Attempt {attempt}: LLM {label} ({'; '.join(changes) or 'no changes'})", llm=True)],
        }

    def _restore_best(state: GenerationState, reason: str) -> dict:
        """Finish with the best attempt (not necessarily the last) and say why the loop stopped."""
        best = state["ir_best"]
        model, report = best["model"], best["report"]
        model.metadata["planner"] = {**model.metadata.get("planner", {}), "history": state["ir_history"],
                                     "attempts": len(state["ir_history"]), "best_attempt": best["attempt"], "stop_reason": reason}
        return {
            "merged_model": model, "validation": report, "next_after_validate": "finalize",
            "topology_proposal": best["topology"], "instrumentation_proposal": best["instrumentation"],
            "decisions": [Decision(agent=primary.AGENT,
                                   summary=f"LLM planner stopped ({reason}) — kept attempt {best['attempt']} with {len(best['errors'])} error(s); human review required",
                                   detail="; ".join(i.message for i in best["errors"][:6]))],
        }

    def route_after_planner(state: GenerationState) -> str:
        if state.get("ir") is not None:
            return "compile_ir"
        return "finalize" if state.get("ir_best") else "topology_agent"

    def compile_ir_node(state: GenerationState) -> dict:
        compiled = compile_plan(state["ir"], _ctx(state, std), state["process"], state["equipment"])
        model = _assemble(state, compiled.topology, compiled.instrumentation)
        model.metadata["engine"] = "llm_planner"
        return {
            "merged_model": model, "engine": "llm_planner",
            "topology_proposal": compiled.topology, "instrumentation_proposal": compiled.instrumentation,
            "ir_errors": compiled.errors, "connection_of_line": compiled.connection_of_line, "node_of_tag": compiled.node_of_tag,
            "events": [_event("compile_ir", f"Plan compiled deterministically — {len(compiled.errors)} plan error(s)")],
        }

    def validate_node(state: GenerationState) -> dict:
        report = validate(state["merged_model"], std, state["source"])
        if state.get("engine") == "llm_planner":
            return _validate_plan(state, report)
        iteration = state.get("iteration", 0)
        target = primary.replan_target(report, iteration)
        s = report.summary()
        return {
            "validation": report,
            "replan_target": target,
            "next_after_validate": "replan" if target else "finalize",
            "events": [
                _event(
                    "validate",
                    f"Engineering rules checked — {s['errors']} error(s), {s['warnings']} warning(s)"
                    + (f"; replanning via {target}" if target else ""),
                    passed=report.passed,
                )
            ],
        }

    def _validate_plan(state: GenerationState, report: ValidationReport) -> dict:
        """Same validator; plan errors from the compiler count too. Errors → back to the planner.

        The loop keeps the best attempt (fewest errors, then warnings), corrects that one, stops early
        when corrections stop helping, and always finishes with the best attempt — never a worse one."""
        report.issues = list(state.get("ir_errors", [])) + report.issues
        attempt = state.get("ir_attempt", 1)
        errors, warnings = sorted(report.errors, key=planner_agent.fix_order), report.warnings
        ctx = _ctx(state, std)
        col, nodes = state.get("connection_of_line", {}), state.get("node_of_tag", {})
        hints = {n: h for n, i in enumerate(errors) if (h := planner_agent.expected_location(i, state["merged_model"], ctx, col, nodes))}

        if str(state.get("ir_final", "")).startswith(REPAIRED):  # the closer's result, compiled once more
            return _finish_repaired(state, report, errors, warnings)
        if state.get("ir_final"):  # the best version of every section, compiled once more for review
            return _finish_combined(state, report, errors, warnings)

        seen = {k: list(v) for k, v in state.get("ir_seen", {}).items()}
        for i in errors:
            seen.setdefault(planner_agent.signature(i), []).append(attempt)

        # ---- per section: its own errors, and its best version so far ----------------------------------
        sections = planner_agent.module_keys(state["process"])
        stage_mod = {st.id: st.module or "" for st in state["process"].stages}
        module_of_tag = {e.tag: stage_mod.get(e.stage, "") for e in state["merged_model"].equipment}
        module_of_tag |= {n.tag: stage_mod.get(n.stage or "", "") for n in state["merged_model"].piping_nodes if n.stage}

        def split(issues):
            out = {m: [] for m in sections}
            for k, i in enumerate(issues):
                for m in planner_agent.modules_of_issue(i, module_of_tag, col, nodes, sections):
                    out[m].append((k, i))
            return out

        by_err, by_warn = split(errors), split(warnings)
        best_sec = dict(state.get("ir_section_best", {}))
        current = state.get("ir_sections") or {sections[0]: state["ir"]}
        counts = {}
        for m in sections:
            errs = [i for _k, i in by_err[m]]
            counts[m] = len(errs)
            sc = (len(errs), len(by_warn[m]))
            prev = best_sec.get(m)
            if prev is None or sc < prev["score"]:
                best_sec[m] = {
                    "plan": current.get(m, planner_agent.EMPTY_PLAN), "attempt": attempt, "score": sc, "count": len(errs),
                    "errors": errs, "warnings": [i for _k, i in by_warn[m]], "col": col, "nodes": nodes,
                    "hints": {pos: hints[k] for pos, (k, _i) in enumerate(by_err[m]) if k in hints},
                }
        prev_best = state.get("ir_best")
        score = (len(errors), len(warnings))
        improved = prev_best is None or score < prev_best["score"]
        best = {
            "attempt": attempt, "score": score, "plan": state["ir"], "model": state["merged_model"], "report": report,
            "errors": errors, "warnings": warnings, "hints": hints, "connection_of_line": col, "node_of_tag": nodes,
            "topology": state["topology_proposal"], "instrumentation": state["instrumentation_proposal"],
        } if improved else prev_best
        history = list(state.get("ir_history", [])) + [{
            "attempt": attempt,
            "plan": planner_agent.summarize(state["ir"]),
            "changes": state.get("ir_changes", ""),
            "errors": [f"[{i.code}] {i.message}" for i in errors],  # hints are added in the feedback only
            "warnings": len(warnings),
            "kept": improved,
            "patch_problems": state.get("ir_patch_problems", []),
        }]
        planner_meta = {"attempts": attempt, "max_attempts": planner_agent.MAX_ATTEMPTS, "history": history,
                        "best_attempt": best["attempt"], "final_plan": planner_agent.plan_json(best["plan"])}
        best["model"].metadata["planner"] = planner_meta
        state["merged_model"].metadata["planner"] = planner_meta
        base = {"ir_errors": errors, "ir_warnings": warnings, "ir_history": history, "ir_hints": hints, "ir_best": best, "ir_seen": seen,
                "ir_section_best": best_sec, "ir_section_counts": counts}

        if report.passed:
            planner_meta["stop_reason"] = "passed validation"
            return {**base, "validation": report, "replan_target": None, "next_after_validate": "finalize",
                    "decisions": [Decision(agent=primary.AGENT, summary=f"LLM plan accepted on attempt {attempt}",
                                           detail=planner_agent.summarize(state["ir"]))],
                    "events": [_event("validate", f"Attempt {attempt}: plan passed validation ({len(warnings)} warning(s))", passed=True)]}
        # Stalling stops the loop only when there is nothing left to try: a section that still has errors
        # and has not used its fresh plans is one such thing, and a fresh plan gets a few corrections of its own.
        replans = state.get("ir_replans", {})
        untried = [m for m in sections if best_sec[m]["errors"] and replans.get(m, 0) < planner_agent.SECTION_REPLANS]
        grace = attempt - state.get("ir_last_replan", 0) < planner_agent.REPLAN_GRACE
        stalled = attempt - best["attempt"]
        stop = (f"reached the limit of {planner_agent.MAX_ATTEMPTS} attempts" if attempt >= planner_agent.MAX_ATTEMPTS
                else f"no improvement in {stalled} corrections" if stalled >= planner_agent.NO_PROGRESS_LIMIT
                and not untried and not grace else None)
        if stop:
            combined = planner_agent.merge_plans({m: best_sec[m]["plan"] for m in sections})
            if len(sections) > 1 and combined.model_dump() != best["plan"].model_dump():
                # sections improved at different attempts: compile their best versions together once more
                return {**base, "ir": combined, "ir_final": stop, "next_after_validate": "compile_ir", "replan_target": None,
                        "events": [_event("validate", f"Attempt {attempt}: {len(errors)} error(s) — {stop}; combining the best "
                                                      f"version of each section for review", passed=False)]}
            fix = _repair({**state, **base}, best["plan"], best["model"], best["errors"],  # type: ignore[arg-type]
                          best["connection_of_line"], best["node_of_tag"], stop, best["score"])
            if fix:
                return {**base, **fix}
            restored = _restore_best({**state, **base}, stop)  # type: ignore[arg-type]
            return {**base, **restored, "replan_target": None,
                    "events": [_event("validate", f"Attempt {attempt}: {len(errors)} error(s) — {stop}; kept attempt {best['attempt']} "
                                                  f"({len(best['errors'])} error(s)) for human review", passed=False)]}
        verdict = (f"{len(errors)} error(s) — best so far" if improved
                   else f"{len(errors)} error(s) — no better than attempt {best['attempt']} ({len(best['errors'])}), discarded")
        return {**base, "validation": report, "replan_target": None, "next_after_validate": "llm_planner",
                "decisions": [Decision(agent=primary.AGENT, summary=f"LLM plan attempt {attempt} rejected: {verdict}",
                                       detail="; ".join(i.message for i in errors[:6]))],
                "events": [_event("validate", f"Attempt {attempt}: {verdict} — sent back to the planner", passed=False)]}

    def _repair(state: GenerationState, plan, model, errors, col, nodes, reason: str, score) -> dict | None:
        """Complete the rule items the planner left behind (deterministic), then validate that once more."""
        if not errors:
            return None
        repaired, applied = planner_agent.auto_fix(plan, errors, model, _ctx(state, std), col, nodes)
        if not applied:
            return None
        return {
            "ir": repaired, "ir_final": f"{REPAIRED}{reason}", "replan_target": None, "next_after_validate": "compile_ir",
            "ir_repair": {"applied": applied, "score": score, "reason": reason},
            "events": [_event("compile_ir", f"{len(applied)} missing rule item(s) completed by the system "
                                            f"({'; '.join(applied[:4])}{'…' if len(applied) > 4 else ''}) — checking again")],
        }

    def _finish_repaired(state: GenerationState, report: ValidationReport, errors, warnings) -> dict:
        """Keep the completed plan when it is better than what the planner reached on its own."""
        rep_meta = state["ir_repair"]
        reason = f"{rep_meta['reason']}; {len(rep_meta['applied'])} rule item(s) completed by the system"
        if (len(errors), len(warnings)) > rep_meta["score"]:  # completing them made it worse: keep the planner's best
            restored = _restore_best(state, rep_meta["reason"])
            return {**restored, "ir_final": "", "replan_target": None,
                    "events": [_event("validate", f"Completing the rule items did not help ({len(errors)} error(s)) — "
                                                  f"kept the planner's best version", passed=False)]}
        model = state["merged_model"]
        model.metadata["planner"] = {**model.metadata.get("planner", {}), "history": state["ir_history"],
                                     "attempts": len(state["ir_history"]), "max_attempts": planner_agent.MAX_ATTEMPTS,
                                     "best_attempt": state["ir_best"]["attempt"], "stop_reason": reason,
                                     "auto_completed": rep_meta["applied"],
                                     "final_plan": planner_agent.plan_json(state["ir"])}
        status = "passed validation" if report.passed else f"{len(errors)} error(s); human review required"
        return {"validation": report, "replan_target": None, "next_after_validate": "finalize", "ir_final": "",
                "decisions": [Decision(agent=primary.AGENT,
                                       summary=f"LLM planner stopped ({rep_meta['reason']}); the system completed "
                                               f"{len(rep_meta['applied'])} missing rule item(s): {status}",
                                       detail="; ".join(rep_meta["applied"][:8]))],
                "events": [_event("validate", f"Rule items completed — {status}", passed=report.passed)]}

    def _finish_combined(state: GenerationState, report: ValidationReport, errors, warnings) -> dict:
        """The combined best sections, validated: keep it unless the best single attempt was better."""
        best = state["ir_best"]
        reason = state["ir_final"]
        if (len(errors), len(warnings)) > best["score"]:
            fix = _repair(state, best["plan"], best["model"], best["errors"], best["connection_of_line"],
                          best["node_of_tag"], reason, best["score"])
            if fix:
                return fix
            restored = _restore_best(state, reason)
            return {**restored, "ir_final": "", "replan_target": None,
                    "events": [_event("validate", f"Combined sections: {len(errors)} error(s) — attempt {best['attempt']} "
                                                  f"({len(best['errors'])}) kept for human review", passed=False)]}
        fix = _repair(state, state["ir"], state["merged_model"], errors, state.get("connection_of_line", {}),
                      state.get("node_of_tag", {}), reason, (len(errors), len(warnings)))
        if fix:
            return fix
        history = state["ir_history"]
        model = state["merged_model"]
        model.metadata["planner"] = {"attempts": len(history), "max_attempts": planner_agent.MAX_ATTEMPTS, "history": history,
                                     "best_attempt": len(history), "combined_sections": True, "stop_reason": reason,
                                     "final_plan": planner_agent.plan_json(state["ir"])}
        status = "passed validation" if report.passed else f"{len(errors)} error(s); human review required"
        return {"validation": report, "replan_target": None, "next_after_validate": "finalize", "ir_final": "",
                "decisions": [Decision(agent=primary.AGENT,
                                       summary=f"LLM planner stopped ({reason}) — best version of each section combined: {status}",
                                       detail="; ".join(i.message for i in errors[:6]))],
                "events": [_event("validate", f"Best version of each section combined — {status}", passed=report.passed)]}

    def replan_node(state: GenerationState) -> dict:
        report = state["validation"]
        return {
            "feedback": report.errors,
            "iteration": state.get("iteration", 0) + 1,
            "decisions": [
                Decision(
                    agent=primary.AGENT,
                    summary=f"Replan: {len(report.errors)} validation error(s) routed to {state['replan_target']}",
                    detail="; ".join(i.message for i in report.errors[:5]),
                )
            ],
        }

    def route_after_validate(state: GenerationState) -> str:
        return state.get("next_after_validate") or ("replan" if state.get("replan_target") else "finalize")

    def route_after_replan(state: GenerationState) -> str:
        t = state["replan_target"]
        return {"process_agent": "plan", "equipment_agent": "plan", "topology_agent": "topology_agent"}.get(t, "instrumentation_agent")  # type: ignore[arg-type]

    def finalize_node(state: GenerationState) -> dict:
        report = state["validation"]
        model = state["merged_model"]
        model.metadata["decisions"] = [d.model_dump() for d in state.get("decisions", [])]
        model.metadata["validation"] = report.summary()
        status = "ready_for_review" if report.passed else "needs_attention"
        return {"status": status, "events": [_event("commit", f"Model committed ({status.replace('_', ' ')})")]}

    def layout_node(state: GenerationState) -> dict:
        model = state["merged_model"]
        model.layout = layout(model, std, state["intent"])
        return {"merged_model": model, "events": [_event("layout", "Deterministic layout computed")]}

    def render_node(state: GenerationState) -> dict:
        svg = render_svg(state["merged_model"], std, revision=state.get("revision", "A"))
        c = state["merged_model"].counts()
        return {
            "svg": svg,
            "events": [_event("render", f"P&ID generated — {c['equipment']} equipment, {c['lines']} lines, {c['instruments']} instruments, {c['control_loops']} control loops", counts=c)],
        }

    g = StateGraph(GenerationState)
    g.add_node("plan", plan)
    g.add_node("process_agent", process_node)
    g.add_node("equipment_agent", equipment_node)
    g.add_node("reconcile", reconcile_node)
    g.add_node("topology_agent", topology_node)
    g.add_node("instrumentation_agent", instrumentation_node)
    g.add_node("merge", merge_node)
    g.add_node("llm_planner", llm_planner_node)
    g.add_node("compile_ir", compile_ir_node)
    g.add_node("validate", validate_node)
    g.add_node("replan", replan_node)
    g.add_node("finalize", finalize_node)
    g.add_node("layout", layout_node)
    g.add_node("render", render_node)

    g.add_edge(START, "plan")
    g.add_edge("plan", "process_agent")
    g.add_edge("plan", "equipment_agent")
    g.add_edge(["process_agent", "equipment_agent"], "reconcile")
    g.add_conditional_edges("reconcile", route_after_reconcile, {"topology_agent": "topology_agent", "llm_planner": "llm_planner"})
    g.add_conditional_edges("llm_planner", route_after_planner, {"compile_ir": "compile_ir", "topology_agent": "topology_agent", "finalize": "finalize"})
    g.add_edge("compile_ir", "validate")
    g.add_edge("topology_agent", "instrumentation_agent")
    g.add_edge("instrumentation_agent", "merge")
    g.add_edge("merge", "validate")
    g.add_conditional_edges("validate", route_after_validate, {"replan": "replan", "finalize": "finalize", "llm_planner": "llm_planner",
                                                                "compile_ir": "compile_ir"})
    g.add_conditional_edges(
        "replan",
        route_after_replan,
        {"plan": "plan", "topology_agent": "topology_agent", "instrumentation_agent": "instrumentation_agent"},
    )
    g.add_edge("finalize", "layout")
    g.add_edge("layout", "render")
    g.add_edge("render", END)
    return g.compile()


def initial_state(
    project: ProjectInfo,
    source: SourceData,
    intent: DesignIntent,
    registry: TagRegistry,
    revision: str = "A",
) -> GenerationState:
    return {
        "project": project,
        "source": source,
        "intent": intent,
        "registry": registry,
        "revision": revision,
        "iteration": 0,
        "decisions": [],
        "events": [],
    }
