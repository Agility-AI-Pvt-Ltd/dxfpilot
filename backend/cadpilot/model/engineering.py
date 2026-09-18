"""The internal engineering model: the source of truth that every drawing is derived from.

The drawing is never edited directly. Agents propose, the merge engine assembles this model,
the validator checks it, and layout/rendering read from it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Quantity(BaseModel):
    value: float
    unit: str

    def __str__(self) -> str:
        v = int(self.value) if float(self.value).is_integer() else self.value
        return f"{v} {self.unit}"


class PortRef(BaseModel):
    item: str
    port: str

    def __str__(self) -> str:
        return f"{self.item}.{self.port}"


class Provenance(BaseModel):
    """Why an object exists: which agent created it, from which rule/source, and why."""

    agent: str
    rule: str | None = None
    source: str | None = None
    rationale: str = ""


class Equipment(BaseModel):
    tag: str
    type: str  # engineering class == symbol library id (pump, silo, plate_heat_exchanger, ...)
    name: str
    stage: str  # process stage id this equipment performs
    area: str
    train: int | None = None  # 1-based train index for per-train stages
    capacity: Quantity | None = None
    attributes: dict[str, str | float | int | bool] = Field(default_factory=dict)
    provenance: Provenance


class PipingNode(BaseModel):
    """Non-equipment topology nodes: headers/manifolds, tees and battery-limit connectors."""

    tag: str
    kind: Literal["header", "tee", "terminal_in", "terminal_out"]
    label: str = ""
    area: str
    stage: str | None = None
    train: int | None = None
    provenance: Provenance


class Stream(BaseModel):
    id: str
    name: str
    service: str  # line service code, e.g. RM (raw milk)
    fluid: str
    design_flow: Quantity  # per train
    properties: dict[str, float] = Field(default_factory=dict)  # fat, snf, temperature ...


class InlineComponent(BaseModel):
    """Something physically installed in a line: a valve or an inline instrument element."""

    tag: str
    type: str  # symbol id: gate_valve, butterfly_valve, check_valve, control_valve, flow_element ...
    position: float = 0.5  # 0..1 along the line, used by layout only
    provenance: Provenance


class Line(BaseModel):
    tag: str
    from_: PortRef = Field(alias="from")
    to: PortRef
    service: str
    stream: str | None = None
    kind: Literal["process", "bypass", "utility"] = "process"
    design_flow: Quantity | None = None
    size_dn: int | None = None
    spec: str = ""
    inline: list[InlineComponent] = Field(default_factory=list)
    provenance: Provenance

    model_config = {"populate_by_name": True}


class InstrumentAttachment(BaseModel):
    kind: Literal["equipment", "line", "piping_node"]
    ref: str
    port: str | None = None


class Instrument(BaseModel):
    tag: str
    function: str  # ISA-style letters: LT, TT, FT, PI, PDI, TIC, FIC ...
    type: str  # symbol id: instrument_field, instrument_dcs, control_valve ...
    location: Literal["field", "dcs", "local_panel"] = "field"
    attached_to: InstrumentAttachment
    loop: str | None = None
    alarms: list[str] = Field(default_factory=list)
    provenance: Provenance


class ControlLoop(BaseModel):
    tag: str  # controller tag, e.g. TIC-201
    measured_by: str  # transmitter tag
    final_element: str  # control valve tag or equipment tag (VFD)
    final_element_kind: Literal["valve", "vfd"]
    setpoint: str = ""
    provenance: Provenance


class ProcessStage(BaseModel):
    id: str
    name: str
    kind: Literal["equipment", "terminal_in", "terminal_out"]
    group: str
    area: str
    equipment_type: str | None = None
    function: str = ""
    outlet_service: str | None = None
    optional: bool = False


class ProcessDefinition(BaseModel):
    template: str
    name: str
    stages: list[ProcessStage]
    groups: dict[str, int]  # train group -> number of parallel units
    basis: dict[str, float | str] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)


class LayoutPoint(BaseModel):
    x: float
    y: float


class Layout(BaseModel):
    positions: dict[str, LayoutPoint] = Field(default_factory=dict)
    width: float = 0
    height: float = 0


class Decision(BaseModel):
    agent: str
    summary: str
    detail: str = ""


class ProjectInfo(BaseModel):
    id: str
    name: str
    plant: str = ""
    drawing_number: str = ""
    standards_profile: str = "international-baseline"


class EngineeringModel(BaseModel):
    schema_version: str = "cadpilot-model/0.1"
    project: ProjectInfo
    process: ProcessDefinition
    equipment: list[Equipment] = Field(default_factory=list)
    piping_nodes: list[PipingNode] = Field(default_factory=list)
    streams: list[Stream] = Field(default_factory=list)
    lines: list[Line] = Field(default_factory=list)
    instruments: list[Instrument] = Field(default_factory=list)
    loops: list[ControlLoop] = Field(default_factory=list)
    layout: Layout = Field(default_factory=Layout)
    metadata: dict[str, object] = Field(default_factory=dict)

    # ---- lookups -------------------------------------------------------------------------
    def node_tags(self) -> set[str]:
        return {e.tag for e in self.equipment} | {n.tag for n in self.piping_nodes}

    def get_equipment(self, tag: str) -> Equipment | None:
        return next((e for e in self.equipment if e.tag == tag), None)

    def get_line(self, tag: str) -> Line | None:
        return next((ln for ln in self.lines if ln.tag == tag), None)

    def get_instrument(self, tag: str) -> Instrument | None:
        return next((i for i in self.instruments if i.tag == tag), None)

    def find(self, tag: str) -> tuple[str, BaseModel] | None:
        for kind, items in (
            ("equipment", self.equipment),
            ("piping_node", self.piping_nodes),
            ("line", self.lines),
            ("instrument", self.instruments),
            ("loop", self.loops),
        ):
            for item in items:
                if item.tag == tag:  # type: ignore[attr-defined]
                    return kind, item
        for ln in self.lines:
            for comp in ln.inline:
                if comp.tag == tag:
                    return "inline", comp
        return None

    def lines_into(self, tag: str) -> list[Line]:
        return [ln for ln in self.lines if ln.to.item == tag]

    def lines_out_of(self, tag: str) -> list[Line]:
        return [ln for ln in self.lines if ln.from_.item == tag]

    def counts(self) -> dict[str, int]:
        return {
            "equipment": len(self.equipment),
            "lines": len(self.lines),
            "instruments": len(self.instruments),
            "control_loops": len(self.loops),
            "valves": sum(1 for ln in self.lines for c in ln.inline if "valve" in c.type),
        }
