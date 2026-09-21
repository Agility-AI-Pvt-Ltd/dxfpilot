"""Primary Agent — the engineering project manager. It plans, delegates, reconciles, decides and
routes replans. It never drafts engineering content itself."""

from __future__ import annotations

from ..model.engineering import Decision
from ..model.proposals import ValidationReport
from ..standards import MODULE_PREFIX, Standards, module_template_id

AGENT = "primary_agent"
WORKER_ORDER = ["process_agent", "equipment_agent", "topology_agent", "instrumentation_agent"]
MAX_REPLANS = 2


WHOLE_PLANT = ("whole plant", "full plant", "entire plant", "complete plant", "all modules", "whole dairy", "complete dairy")
CLASSIC_SCOPE = {"reception", "pasteurization"}  # the classic template already covers these two


def choose_template(request: str, standards: Standards, current: str) -> tuple[str, Decision]:
    """Scope of the drawing: a classic template, or a composition of engineering modules.

    A project whose scope is already a module composition keeps it (the reviewer changes modules
    explicitly, from the UI or in chat). Otherwise the request decides: naming a plant section that
    the classic template does not cover (CIP, steam, curd, ...) switches to engineering modules."""
    if current.startswith(MODULE_PREFIX):
        tpl = standards.template(current)
        return current, Decision(agent=AGENT, summary=f"Scope: {tpl['name']}", detail="engineering modules chosen for this project")
    text = request.lower()
    hits = [mid for mid, m in standards.modules.items() if any(k in text for k in m.get("keywords", []))]
    if any(w in text for w in WHOLE_PLANT):
        hits = list(standards.modules)
    if hits and not set(hits) <= CLASSIC_SCOPE:
        tid = module_template_id(hits, standards)
        tpl = standards.template(tid)
        return tid, Decision(agent=AGENT, summary=f"Scope: {tpl['name']}",
                             detail=f"engineering modules named in the request: {', '.join(tpl['modules'])}")
    best, score = current, 0
    for tid, tpl in standards.process_templates["templates"].items():
        s = sum(1 for k in tpl.get("keywords", []) if k in text)
        if s > score:
            best, score = tid, s
    tpl = standards.template(best)
    detail = f"matched {score} keyword(s) in the request" if score else "default template for this project"
    return best, Decision(agent=AGENT, summary=f"Scope: {tpl['name']}", detail=detail)


def replan_target(report: ValidationReport, iteration: int) -> str | None:
    """Earliest worker in the pipeline that owns an error; None → commit."""
    if report.passed or iteration >= MAX_REPLANS:
        return None
    owners = {i.owner for i in report.errors if i.owner}
    for w in WORKER_ORDER:
        if w in owners:
            return w
    return None
