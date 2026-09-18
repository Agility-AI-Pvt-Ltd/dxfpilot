"""Design intent and structured change operations.

Human corrections never edit the drawing. They are converted to ChangeOps, which are applied
to the DesignIntent. The pipeline regenerates the engineering model from sources + intent, so a
correction is persistent engineering memory: it survives every later regeneration.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from .engineering import Quantity


class EquipmentOverride(BaseModel):
    capacity: Quantity | None = None
    name: str | None = None
    attributes: dict[str, str | float | int | bool] = Field(default_factory=dict)


class AddedInstrument(BaseModel):
    function: str  # PT, TT, FT, PI ...
    attached_kind: Literal["equipment", "line"]
    attached_ref: str  # tag
    rationale: str = "Requested by reviewer"


class DesignIntent(BaseModel):
    template: str = "milk_reception_to_pasteurization"
    request: str = ""
    stage_order: list[str] | None = None  # explicit order override of template stages
    disabled_stages: list[str] = Field(default_factory=list)
    enabled_stages: list[str] = Field(default_factory=list)  # optional stages re-enabled
    group_counts: dict[str, int] = Field(default_factory=dict)
    equipment_overrides: dict[str, EquipmentOverride] = Field(default_factory=dict)
    bypasses: list[str] = Field(default_factory=list)  # equipment tags that get a bypass
    added_instruments: list[AddedInstrument] = Field(default_factory=list)
    suppressed: dict[str, str] = Field(default_factory=dict)  # tag -> reviewer reason (rule waiver)
    layout_offsets: dict[str, tuple[float, float]] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    # Validated LLM answers keyed by a hash of their inputs: regenerations reuse them, so a correction
    # never silently changes an unrelated LLM decision (and does not pay for the same call twice).
    llm_memory: dict[str, dict] = Field(default_factory=dict)


# ---- change operations -------------------------------------------------------------------


class ReorderStage(BaseModel):
    op: Literal["reorder"] = "reorder"
    object: str = Field(description="Equipment tag or stage id to move")
    before: str | None = Field(default=None, description="Place it immediately before this tag/stage")
    after: str | None = Field(default=None, description="Place it immediately after this tag/stage")


class SetCapacity(BaseModel):
    op: Literal["set_capacity"] = "set_capacity"
    tag: str
    value: float
    unit: str


class SetAttribute(BaseModel):
    op: Literal["set_attribute"] = "set_attribute"
    tag: str
    field: str
    value: str


class SetGroupCount(BaseModel):
    op: Literal["set_group_count"] = "set_group_count"
    group: str = Field(description="Train group, e.g. reception, storage, processing")
    count: int


class AddBypass(BaseModel):
    op: Literal["add_bypass"] = "add_bypass"
    around: str = Field(description="Equipment tag to bypass")


class RemoveBypass(BaseModel):
    op: Literal["remove_bypass"] = "remove_bypass"
    around: str


class ToggleStage(BaseModel):
    op: Literal["disable_stage", "enable_stage"]
    stage: str = Field(description="Stage id or an equipment tag in that stage")


class AddInstrument(BaseModel):
    op: Literal["add_instrument"] = "add_instrument"
    function: str
    attached_to: str = Field(description="Equipment or line tag")
    rationale: str = "Requested by reviewer"


class RemoveItem(BaseModel):
    op: Literal["remove_item"] = "remove_item"
    tag: str
    reason: str = "Removed by reviewer"


class MoveItem(BaseModel):
    op: Literal["move"] = "move"
    tag: str
    dx: float = 0
    dy: float = 0


class Explain(BaseModel):
    op: Literal["explain"] = "explain"
    tag: str


ChangeOp = Annotated[
    Union[
        ReorderStage,
        SetCapacity,
        SetAttribute,
        SetGroupCount,
        AddBypass,
        RemoveBypass,
        ToggleStage,
        AddInstrument,
        RemoveItem,
        MoveItem,
        Explain,
    ],
    Field(discriminator="op"),
]


# The same operations for LLM structured output. OpenAI's strict JSON schema does not allow the
# `oneOf` a discriminated union produces; a plain union (`anyOf`) still validates by the `op` literal.
LLMChangeOp = Union[
    ReorderStage,
    SetCapacity,
    SetAttribute,
    SetGroupCount,
    AddBypass,
    RemoveBypass,
    ToggleStage,
    AddInstrument,
    RemoveItem,
    MoveItem,
    Explain,
]


class ChangeRequest(BaseModel):
    """A human correction, interpreted into structured operations."""

    message: str
    operations: list[ChangeOp] = Field(default_factory=list)
    clarification: str | None = None  # set when the interpreter cannot act without more info
    interpreter: str = "rules"
