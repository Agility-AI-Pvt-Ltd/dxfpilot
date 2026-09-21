"""Structured worker-agent outputs and validation results.

Workers never return prose to the primary agent — they return these proposals.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .engineering import (
    ControlLoop,
    Equipment,
    Instrument,
    InlineComponent,
    Line,
    PipingNode,
    ProcessDefinition,
    Stream,
)


class Proposal(BaseModel):
    agent: str
    status: Literal["proposed", "failed"] = "proposed"
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    used_llm: bool = False


class ProcessProposal(Proposal):
    agent: str = "process_agent"
    process: ProcessDefinition
    streams: list[Stream] = Field(default_factory=list)


class EquipmentProposal(Proposal):
    agent: str = "equipment_agent"
    equipment: list[Equipment] = Field(default_factory=list)
    group_counts: dict[str, int] = Field(default_factory=dict)
    source_rows: dict[str, str] = Field(default_factory=dict)  # stage id -> matched source row
    sizing: list[dict] = Field(default_factory=list)  # mass-balance sizing per stage (engine.selection)


class TopologyProposal(Proposal):
    agent: str = "topology_agent"
    piping_nodes: list[PipingNode] = Field(default_factory=list)
    lines: list[Line] = Field(default_factory=list)


class InstrumentationProposal(Proposal):
    agent: str = "instrumentation_agent"
    instruments: list[Instrument] = Field(default_factory=list)
    loops: list[ControlLoop] = Field(default_factory=list)
    inline: dict[str, list[InlineComponent]] = Field(default_factory=dict)  # line tag -> components


Severity = Literal["error", "warning", "info"]
Layer = Literal["structural", "topology", "engineering", "data", "exchange"]


class ValidationIssue(BaseModel):
    layer: Layer
    severity: Severity
    code: str
    message: str
    refs: list[str] = Field(default_factory=list)
    owner: str | None = None  # worker agent responsible for fixing it during replan
    rule: str | None = None


class ValidationReport(BaseModel):
    issues: list[ValidationIssue] = Field(default_factory=list)
    checks_run: dict[str, int] = Field(default_factory=dict)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def summary(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "checks_run": self.checks_run,
        }
