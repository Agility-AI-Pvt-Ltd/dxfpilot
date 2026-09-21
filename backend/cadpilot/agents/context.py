from __future__ import annotations

from dataclasses import dataclass, field

from ..engine.tags import Tagger, TagRegistry
from ..ingest.excel import SourceData
from ..model.intent import DesignIntent
from ..model.proposals import ValidationIssue
from ..standards import Standards


@dataclass
class AgentContext:
    standards: Standards
    source: SourceData
    intent: DesignIntent
    registry: TagRegistry
    feedback: list[ValidationIssue] = field(default_factory=list)  # validator issues during replan

    @property
    def tagger(self) -> Tagger:
        return Tagger(self.standards, self.registry)

    @property
    def template(self) -> dict:
        return self.standards.template(self.intent.template)

    def trace_summary(self) -> dict:
        """Compact span input for LangSmith (the full source data is too large to trace per agent)."""
        return {
            "request": self.intent.request,
            "template": self.intent.template,
            "design_data_rows": len(self.source.equipment_rows),
            "replan_feedback": [i.message for i in self.feedback],
        }

    def feedback_for(self, agent: str) -> list[ValidationIssue]:
        return [i for i in self.feedback if i.owner == agent]
