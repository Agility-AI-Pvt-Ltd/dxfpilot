"""Correction interpreter — turns a reviewer's chat message into structured ChangeOps.

With an LLM: structured output constrained to the ChangeOp schema. Without one: a rule-based
parser for the common correction phrasings. Either way the result is data, never free text.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..model.engineering import EngineeringModel
from ..model.intent import (
    AddBypass,
    AddInstrument,
    ChangeOp,
    ChangeRequest,
    LLMChangeOp,
    Explain,
    MoveItem,
    RemoveBypass,
    RemoveItem,
    ReorderStage,
    SetCapacity,
    SetGroupCount,
    ToggleStage,
)
from langsmith import traceable

from .llm import get_llm, mark_source

TAG_RE = r"[A-Z]{1,4}-\d{2,4}"
_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_GROUP_WORDS = {
    "silo": "storage", "rmst": "storage", "storage": "storage", "tank": "storage",
    "reception": "reception", "unloading": "reception",
    "processing": "processing", "pasteuri": "processing", "process line": "processing",
}


class _LLMInterpretation(BaseModel):
    operations: list[LLMChangeOp] = Field(description="Structured model changes; empty if nothing should change")
    clarification: str | None = Field(description="A question to ask the reviewer if the request is ambiguous, else null")


def _this(term: str | None, selected: str | None) -> str | None:
    if term is None:
        return selected
    t = term.strip().strip(".")
    return selected if t.lower() in ("this", "it", "this one", "that", "selected") and selected else t


def _find_tag(text: str) -> str | None:
    m = re.search(TAG_RE, text.upper())
    return m.group(0) if m else None


def interpret_rules(message: str, selected: str | None) -> ChangeRequest:
    msg = message.strip()
    low = msg.lower()
    ops: list[ChangeOp] = []

    if re.match(r"^\s*(why|what|explain|tell me)", low):
        tag = _find_tag(msg) or selected
        if tag:
            ops.append(Explain(tag=tag))
            return ChangeRequest(message=message, operations=ops)
        return ChangeRequest(message=message, clarification="Which item do you mean? Select it on the drawing or name its tag.")

    if m := re.search(r"(?:remove|delete|no)\s+(?:the\s+)?bypass(?:\s+(?:around|on|for|across)\s+(?P<t>[\w -]+))?", low):
        t = _this(m.group("t"), selected)
        if t:
            ops.append(RemoveBypass(around=t))

    elif m := re.search(r"add\s+(?:a\s+)?bypass(?:\s+(?:around|on|for|across|to)\s+(?P<t>[\w -]+))?", low):
        t = _this(m.group("t"), selected)
        if not t:
            return ChangeRequest(message=message, clarification="Which equipment should the bypass go around?")
        ops.append(AddBypass(around=t.upper() if re.fullmatch(TAG_RE, t.upper()) else t))

    elif m := re.search(
        r"(?:change|set|make|resize|increase|decrease|update)\s+(?P<t>.+?)\s+(?:capacity\s+)?(?:to|=|at)\s*(?P<v>[\d.]+)\s*(?P<u>klph|kl|lph|l|m3/h)\b",
        low,
    ):
        t = _this(m.group("t").replace("capacity of", "").replace("the ", "").strip(), selected)
        ops.append(SetCapacity(tag=(t or "").upper() if re.fullmatch(TAG_RE, (t or "").upper()) else (t or ""), value=float(m.group("v")), unit=m.group("u")))

    elif m := re.search(r"(?P<n>\d+|one|two|three|four|five|six)\s+(?P<w>silos?|rmsts?|tanks?|reception lines?|unloading lines?|processing lines?|pasteuri[sz]ers?|process lines?)", low):
        n = int(m.group("n")) if m.group("n").isdigit() else _NUM[m.group("n")]
        group = next(g for k, g in _GROUP_WORDS.items() if k in m.group("w"))
        ops.append(SetGroupCount(group=group, count=n))

    elif m := re.search(r"add\s+(?:a|an|one)?\s*(?P<f>[a-z]{2,4})\b.*?\s(?:on|to|at|in)\s+(?P<t>[\w-]+)", msg, re.IGNORECASE):
        t = _this(m.group("t"), selected)
        ops.append(AddInstrument(function=m.group("f").upper(), attached_to=(t or "").upper()))

    elif m := re.search(r"move\s+(?P<t>[\w-]+)?\s*(?P<d>left|right|up|down)(?:\s+by\s+(?P<n>\d+))?", low):
        t = _this(m.group("t"), selected)
        if not t:
            return ChangeRequest(message=message, clarification="Which item should I move? Select it on the drawing.")
        n = float(m.group("n") or 120)
        dx, dy = {"left": (-n, 0), "right": (n, 0), "up": (0, -n), "down": (0, n)}[m.group("d")]
        ops.append(MoveItem(tag=t.upper(), dx=dx, dy=dy))

    elif m := re.search(
        r"(?:move\s+)?(?:the\s+)?(?P<a>[\w -]+?)\s+(?:should|must|needs to|has to)?\s*(?:be|go|come|sit)?\s*(?:placed\s+|located\s+)?(?P<rel>before|after|upstream of|downstream of|ahead of)\s+(?:the\s+)?(?P<b>[\w -]+)",
        low,
    ):
        a = _this(m.group("a"), selected) or m.group("a")
        b = m.group("b")
        before = m.group("rel") in ("before", "upstream of", "ahead of")
        ops.append(ReorderStage(object=a, before=b if before else None, after=None if before else b))

    elif m := re.search(r"(?:remove|delete|drop|don't need|do not need|no need for)\s+(?:the\s+)?(?P<t>[\w -]+)", low):
        t = _this(m.group("t"), selected) or ""
        tag = _find_tag(t) or (t.upper() if re.fullmatch(TAG_RE, t.upper()) else None)
        if tag:
            ops.append(RemoveItem(tag=tag))
        else:
            ops.append(ToggleStage(op="disable_stage", stage=t))

    elif m := re.search(r"(?:add|include|enable)\s+(?:the\s+|a\s+)?(?P<t>chiller|de-?aerat\w*|separator|strainer)", low):
        ops.append(ToggleStage(op="enable_stage", stage=m.group("t")))

    if not ops:
        return ChangeRequest(
            message=message,
            clarification=(
                "I couldn't map that to a model change. I understand corrections like: "
                "'The separator should be before the pasteurizer', 'Change E-201 to 25 KLPH', "
                "'Add a bypass around F-201', 'Use 4 silos', 'Add PT on RM-2003', 'Remove LSHH-111', "
                "'Move T-101 down by 80', 'Why is FT-103 required?'"
            ),
        )
    return ChangeRequest(message=message, operations=ops)


def _model_digest(model: EngineeringModel) -> str:
    stages = ", ".join(f"{s.id} ({s.name}, group {s.group})" for s in model.process.stages)
    eq = "; ".join(f"{e.tag} {e.name} [{e.stage}] {e.capacity or ''}" for e in model.equipment)
    inst = ", ".join(f"{i.tag}@{i.attached_to.ref}" for i in model.instruments)
    lines = ", ".join(f"{ln.tag}:{ln.from_.item}>{ln.to.item}" for ln in model.lines)
    return f"Stage order: {stages}\nGroups: {model.process.groups}\nEquipment: {eq}\nLines: {lines}\nInstruments: {inst}"


_SYSTEM = """You are the correction interpreter of CadPilot, an AI P&ID drafting system. A reviewing \
engineer writes a correction or question about the current P&ID. Convert it into structured \
operations on the engineering model — never drawing instructions.

How the model works, so you do not ask for things CadPilot decides itself:
- Tags are assigned automatically. Never ask the reviewer for a tag of a new item.
- A number of units ("use 4 silos", "3 reception lines", "add a silo") is `set_group_count` on the
  train group — never `set_capacity`. New units copy the capacity and connections of the group.
- `set_capacity` is only for a flow or volume: KLPH, LPH, m3/h, KL, L or kg/h.
- Use existing tags and stage ids exactly as listed. "this"/"it" means the selected item.
- Questions ("why…", "what…") become `explain`.

Act whenever the intent is clear. Only return no operations with a clarification when the request
could reasonably mean two different model changes."""


@traceable(name="correction_interpreter", run_type="chain", process_inputs=lambda d: {"message": d["message"], "selected": d.get("selected")})
def interpret(message: str, model: EngineeringModel, selected: str | None = None) -> ChangeRequest:
    change = _interpret(message, model, selected)
    mark_source(change.interpreter, operations=[op.model_dump() for op in change.operations], clarification=change.clarification)
    return change


def _interpret(message: str, model: EngineeringModel, selected: str | None = None) -> ChangeRequest:
    rules = interpret_rules(message, selected)
    llm = get_llm()
    if llm.enabled:
        result = llm.structured(
            system=_SYSTEM,
            prompt=(
                f"Current model:\n{_model_digest(model)}\n\nTrain groups for set_group_count: "
                f"{', '.join(model.process.groups)}\n\nSelected on drawing: {selected or 'nothing'}\n\n"
                f"Reviewer: {message}"
            ),
            schema=_LLMInterpretation,
            purpose="correction_interpreter",
        )
        if result is not None and result.operations:
            return ChangeRequest(
                message=message, operations=result.operations, clarification=result.clarification, interpreter="llm"
            )
        if result is not None and not rules.operations:
            return ChangeRequest(message=message, clarification=result.clarification, interpreter="llm")
        # The model asked a needless question or gave an unusable reply, but the rules understood it
        why = "model asked a question instead" if result is not None else "model reply unusable"
        return rules.model_copy(update={"interpreter": f"rules ({why})"})
    return rules
