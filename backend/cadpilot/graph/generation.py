"""Autonomous generation workflow (LangGraph).

START → plan → {process ∥ equipment} → reconcile → topology → instrumentation → merge → validate
      → (replan → worker … | finalize) → layout → render → END

Process and Equipment work in parallel; Topology needs both (it connects the equipment in the
process order) and Instrumentation needs the topology (it attaches to lines).
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..agents import equipment as equipment_agent
from ..agents import instrumentation as instrumentation_agent
from ..agents import primary
from ..agents import process as process_agent
from ..agents import topology as topology_agent
from ..agents.context import AgentContext
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

    def merge_node(state: GenerationState) -> dict:
        model = assemble(
            state["project"],
            state["process"],
            state["process_proposal"],
            state["equipment"],
            state["topology_proposal"],
            state["instrumentation_proposal"],
            state["intent"],
            state.get("decisions", []),
        )
        warnings = [
            w
            for p in (state["process_proposal"], state["equipment_proposal"], state["topology_proposal"], state["instrumentation_proposal"])
            for w in p.warnings
        ]
        assumptions = state["process_proposal"].assumptions + state["equipment_proposal"].assumptions
        model.metadata["warnings"] = warnings
        model.metadata["assumptions"] = assumptions
        return {"merged_model": model, "events": [_event("merge", "Proposals merged into the engineering model")]}

    def validate_node(state: GenerationState) -> dict:
        report = validate(state["merged_model"], std, state["source"])
        iteration = state.get("iteration", 0)
        target = primary.replan_target(report, iteration)
        s = report.summary()
        return {
            "validation": report,
            "replan_target": target,
            "events": [
                _event(
                    "validate",
                    f"Engineering rules checked — {s['errors']} error(s), {s['warnings']} warning(s)"
                    + (f"; replanning via {target}" if target else ""),
                    passed=report.passed,
                )
            ],
        }

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
        return "replan" if state.get("replan_target") else "finalize"

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
    g.add_node("validate", validate_node)
    g.add_node("replan", replan_node)
    g.add_node("finalize", finalize_node)
    g.add_node("layout", layout_node)
    g.add_node("render", render_node)

    g.add_edge(START, "plan")
    g.add_edge("plan", "process_agent")
    g.add_edge("plan", "equipment_agent")
    g.add_edge(["process_agent", "equipment_agent"], "reconcile")
    g.add_edge("reconcile", "topology_agent")
    g.add_edge("topology_agent", "instrumentation_agent")
    g.add_edge("instrumentation_agent", "merge")
    g.add_edge("merge", "validate")
    g.add_conditional_edges("validate", route_after_validate, {"replan": "replan", "finalize": "finalize"})
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
