"""Primary Agent — the engineering project manager. It plans, delegates, reconciles, decides and
routes replans. It never drafts engineering content itself."""

from __future__ import annotations

from ..model.engineering import Decision
from ..model.proposals import ValidationReport
from ..standards import Standards

AGENT = "primary_agent"
WORKER_ORDER = ["process_agent", "equipment_agent", "topology_agent", "instrumentation_agent"]
MAX_REPLANS = 2


def choose_template(request: str, standards: Standards, current: str) -> tuple[str, Decision]:
    text = request.lower()
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
