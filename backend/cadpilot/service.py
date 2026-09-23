"""Application service: generation, conversational correction, revisions, approval."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .agents.interpreter import interpret, interpret_rules
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from .agents.llm import LLMCall, record_calls
from .engine.apply import apply_ops
from .engine.render import render_svg
from .engine.revisions import diff_models, next_revision
from .graph.generation import build_generation_graph, initial_state
from .ingest.excel import READER_VERSION, SourceData, TableOverride, load_workbooks, preview_override, sheet_grids
from .model.intent import ChangeRequest, DesignIntent, Explain
from .standards import get_standards, module_template_id
from .store import ChatMessage, ProjectRecord, ProjectStore, Revision, get_store, now_iso

def _trace_meta(**meta: Any) -> None:
    """Attach project context to the current LangSmith trace (no-op without tracing)."""
    run = get_current_run_tree()
    if run is not None:
        run.metadata.update({k: v for k, v in meta.items() if v is not None})


def _rev_out(rev: Any) -> dict[str, Any]:
    return {
        "revision": rev.revision, "status": rev.status, "counts": rev.model.counts(),
        "validation": rev.validation.summary(), "diff": rev.diff_summary,
    }


def _inputs(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in ("self", "on_event")}


DEMO_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEMO_FILES = {
    "mass_balance": DEMO_DATA_DIR / "Mass_Balance_Dummy_Data.xlsx",
    "design_data": DEMO_DATA_DIR / "Dairy_Plant_Estimation_Dummy_Data.xlsx",
}


class CadPilot:
    def __init__(self, store: ProjectStore | None = None):
        self.store = store or get_store()
        self.standards = get_standards()
        self.graph = build_generation_graph(self.standards)

    # ---- projects & sources -----------------------------------------------------------------
    def create_project(self, name: str, demo_data: bool = False) -> ProjectRecord:
        rec = self.store.create(name)
        if demo_data:
            self.set_sources(
                rec.info.id,
                {role: (path.name, path.read_bytes()) for role, path in DEMO_FILES.items()},
            )
        return self.store.get(rec.info.id)

    @traceable(name="cadpilot: read workbooks", run_type="chain", process_inputs=lambda d: {"project": d["pid"], "files": {r: n for r, (n, _) in d["files"].items()}})
    def set_sources(self, pid: str, files: dict[str, tuple[str, bytes]]) -> ProjectRecord:
        """files: role -> (original filename, bytes). Replaces the workbook for each given role."""
        with record_calls() as calls:  # the LLM table mapper may be asked when headers are unrecognised
            try:
                return self._set_sources(pid, files)
            finally:
                self.store.record_llm_calls(pid, None, calls)

    def _set_sources(self, pid: str, files: dict[str, tuple[str, bytes]]) -> ProjectRecord:
        self.store.put_sources(pid, files)
        return self._reingest(pid)

    def _reingest(self, pid: str) -> ProjectRecord:
        """Re-read the stored workbooks, applying the reviewer's column mappings."""
        rec = self.store.get(pid)
        data = load_workbooks(list(self.store.source_files(pid).values()), overrides=rec.table_overrides)
        self.store.save_source_data(pid, data)
        if data.plant_title:
            rec.info.plant = data.plant_title
        self.store.save(rec)
        return rec

    def source_data(self, pid: str) -> SourceData | None:
        """Parsed workbooks; data read by an older version of the reader is re-read once."""
        data = self.store.source_data(pid)
        if data is not None and data.reader_version < READER_VERSION and self.store.source_files(pid):
            self._reread(pid, data.reader_version)
            data = self.store.source_data(pid)
        return data

    @traceable(name="cadpilot: re-read workbooks", run_type="chain", process_inputs=lambda d: {"project": d["pid"], "stored_by_reader_version": d["old_version"], "current_reader_version": READER_VERSION})
    def _reread(self, pid: str, old_version: int) -> None:
        """One-time upgrade of workbooks parsed by an older reader (its LLM calls nest under this trace)."""
        _trace_meta(project_id=pid)
        with record_calls() as calls:
            try:
                self._reingest(pid)
            finally:
                self.store.record_llm_calls(pid, None, calls)

    # ---- reviewer column mappings -------------------------------------------------------------------
    def sheets(self, pid: str) -> list[dict[str, Any]]:
        """Every sheet of the uploaded workbooks (first rows as text) for the mapping editor."""
        files = self.store.source_files(pid)
        role_of = {name: role for role, (name, _) in files.items()}
        return [{**g, "role": role_of.get(g["file"])} for g in sheet_grids(list(files.values()))]

    def preview_mapping(self, pid: str, override: TableOverride) -> dict[str, Any]:
        return preview_override(list(self.store.source_files(pid).values()), override)

    @traceable(name="cadpilot: set column mapping", run_type="chain", process_inputs=lambda d: {"project": d["pid"], "mapping": d["override"].model_dump()})
    def set_mapping(self, pid: str, override: TableOverride) -> ProjectRecord:
        """Save the reviewer's mapping for a table kind (replacing any earlier one) and re-read."""
        rec = self.store.get(pid)
        rec.table_overrides = [o for o in rec.table_overrides if o.kind != override.kind] + [override]
        self.store.save(rec)
        with record_calls() as calls:
            try:
                return self._reingest(pid)
            finally:
                self.store.record_llm_calls(pid, None, calls)

    def clear_mapping(self, pid: str, kind: str) -> ProjectRecord:
        """Back to automatic detection for this table kind."""
        rec = self.store.get(pid)
        rec.table_overrides = [o for o in rec.table_overrides if o.kind != kind]
        self.store.save(rec)
        with record_calls() as calls:
            try:
                return self._reingest(pid)
            finally:
                self.store.record_llm_calls(pid, None, calls)

    # ---- autonomous generation ----------------------------------------------------------------
    @traceable(name="cadpilot: generate draft", run_type="chain", process_inputs=_inputs, process_outputs=_rev_out)
    def run_generation(
        self, pid: str, request: str, on_event=None, engine: str | None = None, modules: list[str] | None = None,
        detect_modules: bool = False,
    ) -> Revision:
        """`modules`: engineering modules to draw (explicit choice; replaces the project's scope).
        `detect_modules`: read the workbooks and draw the plant sections they contain."""
        _trace_meta(project_id=pid, engine=engine, modules=",".join(modules or []), detect_modules=detect_modules)
        with record_calls() as calls:
            rev = None
            try:
                preamble = None
                if detect_modules and modules is None:
                    modules, preamble = self._detect_scope(pid, request, on_event)
                self._set_scope(pid, engine, modules)
                rev = self._run_generation(pid, request, on_event, calls, preamble)
                return rev
            finally:
                self.store.record_llm_calls(pid, rev.revision if rev else None, calls)

    def detect_scope(self, pid: str):
        """Which engineering modules the project's workbooks describe (with the rows as evidence)."""
        from .agents import scope

        rec = self.store.get(pid)
        source = self.store.source_data(pid)
        if source is None:
            raise ValueError("Upload the mass balance and design data first.")
        det = scope.detect(source, get_standards(), memory=rec.intent.llm_memory)  # the answer is cached in the intent
        rec.intent = rec.intent.model_copy(update={"scope_detection": det.model_dump()})
        self.store.save(rec)
        return det

    def _detect_scope(self, pid: str, request: str, on_event) -> tuple[list[str], str]:
        """Detected modules, narrowed to the sections a request names ("only reception and CIP")."""
        std = get_standards()
        det = self.detect_scope(pid)
        text = (request or "").lower()
        named = [mid for mid, m in std.modules.items() if any(k in text for k in m.get("keywords", []))]
        chosen = named or det.selected
        note = det.summary()
        # a utility that is not drawn although the chosen sections need it
        for f in det.modules:
            util = std.modules[f.id].get("utility")
            if not util or f.id in chosen:
                continue
            users = [st["name"] for mid in chosen for st in std.modules[mid]["stages"]
                     if util in [u.strip() for u in str(st.get("attributes", {}).get("utility", "")).split(",")]]
            if users:
                note += (f"\nNote: {len(users)} item(s) in these sections use {util} ({', '.join(dict.fromkeys(users))[:120]}) "
                         f"but the {f.name} section is {'only partly' if f.status == 'partial' else 'not'} in the workbooks — "
                         f"tick {f.name} under Plant sections to draw its supply.")
        if named:
            note += f"\nYour request names {', '.join(std.modules[m]['name'] for m in named)} — drawing only {'that' if len(named) == 1 else 'those'}."
        if not chosen:
            note = "I could not find any plant section of the module library in the workbooks — drawing the classic reception → pasteurization line."
        if on_event:
            on_event({"step": "scope", "detail": f"Plant sections from the workbooks: {', '.join(std.modules[m]['name'] for m in chosen) or 'none'}",
                      "llm": det.used_llm})
        return chosen, note

    def _set_scope(self, pid: str, engine: str | None, modules: list[str] | None) -> None:
        if engine or modules is not None:
            rec = self.store.get(pid)
            if engine and engine not in ("rules", "llm_planner"):
                raise ValueError(f"Unknown engine {engine!r}")
            if engine:
                rec.intent = rec.intent.model_copy(update={"engine": engine})
            if modules == []:  # back to the classic single template
                rec.intent = rec.intent.model_copy(update={"template": DesignIntent().template})
            elif modules:
                std = get_standards()
                unknown = [m for m in modules if m not in std.modules]
                if unknown:
                    raise ValueError(f"Unknown engineering module(s): {', '.join(unknown)} (available: {', '.join(std.modules)})")
                rec.intent = rec.intent.model_copy(update={"template": module_template_id(modules, std)})
            self.store.save(rec)

    @staticmethod
    def _llm_summary(calls: list[LLMCall]) -> list[dict[str, Any]]:
        return [
            {"purpose": c.purpose, "outcome": c.outcome, "model": c.served_model or c.model, "ms": c.latency_ms}
            for c in calls
        ]

    def _run_generation(self, pid: str, request: str, on_event, calls: list[LLMCall], preamble: str | None = None) -> Revision:
        rec = self.store.get(pid)
        source = self.store.source_data(pid)
        if source is None:
            raise ValueError("Upload the mass balance and design data first.")
        intent = rec.intent.model_copy(update={"request": request or rec.intent.request})
        rec.chat.append(ChatMessage(role="user", text=request or "Generate the P&ID"))
        if preamble:  # what the scope agent found in the workbooks, before the draft
            rec.chat.append(ChatMessage(role="assistant", text=preamble, data={"kind": "scope"}))
        final = self._run_graph(rec, source, intent, on_event)
        prev = self.store.revision(pid, rec.current) if rec.current else None
        rev = self._commit(rec, final, intent, None, prev)
        c = rev.model.counts()
        text = (
            f"Draft ready for review — revision {rev.revision}. {c['equipment']} equipment, {c['lines']} lines, "
            f"{c['instruments']} instruments, {c['control_loops']} control loops, {c['valves']} valves. "
            + self._validation_sentence(rev)
        )
        rec.chat.append(
            ChatMessage(
                role="assistant", text=text, revision=rev.revision,
                data={"events": rev.events, "llm": self._llm_summary(calls)},
            )
        )
        self.store.save(rec)
        return rev

    def _run_graph(self, rec: ProjectRecord, source, intent: DesignIntent, on_event=None) -> dict:
        letter = next_revision(rec.current)
        state: dict = dict(initial_state(rec.info, source, intent, rec.registry, letter))
        for update in self.graph.stream(state, stream_mode="updates", config={"recursion_limit": 140}):  # planner loop: up to 14 × 3 steps, plus the combined recompile
            for _node, delta in update.items():
                if not delta:
                    continue
                for k, v in delta.items():
                    if k in ("events", "decisions"):
                        state[k] = state.get(k, []) + v
                    else:
                        state[k] = v
                for ev in delta.get("events", []):
                    if on_event:
                        on_event(ev)
        return state

    def _commit(self, rec: ProjectRecord, state: dict, intent: DesignIntent, change: ChangeRequest | None, prev: Revision | None) -> Revision:
        model = state["merged_model"]
        diff = diff_models(prev.model, model) if prev else None
        rev = Revision(
            revision=next_revision(rec.current),
            status=state["status"],
            model=model,
            intent=state["intent"],
            validation=state["validation"],
            events=state.get("events", []),
            change_request=change,
            diff=diff,
            diff_summary=diff.summary() if diff else "Initial draft.",
        )
        svg = render_svg(model, self.standards, highlight=diff.changed_tags() if diff else set(), revision=rev.revision)
        rec.intent = state["intent"]
        rec.revisions.append(rev.revision)
        supersede = prev.revision if prev and prev.status != "approved" else None
        self.store.commit_revision(rec, rev, svg, supersede)
        return rev

    @staticmethod
    def _validation_sentence(rev: Revision) -> str:
        v = rev.validation
        if v.passed and not v.warnings:
            return "Validation passed."
        if v.passed:
            return f"Validation passed with {len(v.warnings)} warning(s) for your attention."
        return f"Validation found {len(v.errors)} error(s) the agents could not resolve — please review."

    # ---- conversational correction ------------------------------------------------------------
    @traceable(name="cadpilot: review message", run_type="chain", process_inputs=_inputs)
    def chat(self, pid: str, message: str, selected: str | None = None, on_event=None) -> dict[str, Any]:
        _trace_meta(project_id=pid, selected=selected)
        with record_calls() as calls:
            out: dict[str, Any] = {}
            try:
                out = self._chat(pid, message, selected, on_event, calls)
                return out
            finally:
                if out.get("kind") != "generated":  # a first draft records its own calls
                    self.store.record_llm_calls(pid, out.get("revision"), calls)

    def _chat(self, pid: str, message: str, selected: str | None, on_event, calls: list[LLMCall]) -> dict[str, Any]:
        rec = self.store.get(pid)
        if not rec.current:
            rev = self.run_generation(pid, message, on_event)
            return {"reply": self.store.get(pid).chat[-1].text, "revision": rev.revision, "kind": "generated"}

        current = self.store.revision(pid, rec.current)
        rec.chat.append(ChatMessage(role="user", text=message, data={"selected": selected} if selected else {}))
        change = interpret(message, current.model, selected)
        meta = {"interpreter": change.interpreter, "llm": self._llm_summary(calls)}

        if change.clarification and not change.operations:
            rec.chat.append(ChatMessage(role="assistant", text=change.clarification, data=meta))
            self.store.save(rec)
            return {"reply": change.clarification, "kind": "clarification", "change_request": change.model_dump()}

        new_intent, applied, rejected, answers = apply_ops(rec.intent, change.operations, current.model, self.standards)
        if change.interpreter == "llm" and rejected:
            # The model proposed something the engineering model cannot accept; if the rules understand
            # the message and their change applies cleanly, use that instead.
            rules = interpret_rules(message, selected)
            if rules.operations:
                r_intent, r_applied, r_rejected, r_answers = apply_ops(rec.intent, rules.operations, current.model, self.standards)
                if r_applied and not r_rejected:
                    change = rules.model_copy(update={"interpreter": f"rules (model proposal rejected: {rejected[0]})"})
                    new_intent, applied, rejected, answers = r_intent, r_applied, r_rejected, r_answers
                    meta["interpreter"] = change.interpreter
        only_questions = all(isinstance(op, Explain) for op in change.operations)
        if only_questions or not applied:
            reply = " ".join(answers + rejected) or "Nothing to change."
            rec.chat.append(ChatMessage(role="assistant", text=reply, data=meta))
            self.store.save(rec)
            return {"reply": reply, "kind": "answer", "change_request": change.model_dump()}

        rec.corrections.append(change)
        if on_event:
            on_event({"step": "interpret", "detail": "Correction understood: " + " ".join(applied)})
        source = self.store.source_data(pid)
        state = self._run_graph(rec, source, new_intent, on_event)
        rev = self._commit(rec, state, new_intent, change, current)
        reply = " ".join(
            [f"Understood. {' '.join(applied)}"]
            + ([f"Not applied: {' '.join(rejected)}"] if rejected else [])
            + answers
            + [f"Revision {rev.revision} generated — {rev.diff_summary}", self._validation_sentence(rev)]
        )
        rec.chat.append(
            ChatMessage(
                role="assistant", text=reply, revision=rev.revision,
                data={**meta, "events": rev.events, "llm": self._llm_summary(calls)},
            )
        )
        self.store.save(rec)
        return {"reply": reply, "kind": "revision", "revision": rev.revision, "change_request": change.model_dump()}

    @traceable(name="cadpilot: restore revision", run_type="chain", process_inputs=_inputs, process_outputs=_rev_out)
    def restore(self, pid: str, letter: str, on_event=None) -> Revision:
        """Bring back an earlier revision's design as a new revision (history is never rewritten)."""
        _trace_meta(project_id=pid, restored=letter)
        with record_calls() as calls:
            rev = None
            try:
                rev = self._restore(pid, letter, on_event, calls)
                return rev
            finally:
                self.store.record_llm_calls(pid, rev.revision if rev else None, calls)

    def _restore(self, pid: str, letter: str, on_event, calls: list[LLMCall]) -> Revision:
        rec = self.store.get(pid)
        old = self.store.revision(pid, letter)
        if letter == rec.current:
            raise ValueError(f"Revision {letter} is already the current revision.")
        current = self.store.revision(pid, rec.current)
        message = f"Restore revision {letter}"
        rec.chat.append(ChatMessage(role="user", text=message))
        # keep the LLM memory gathered since, so restoring does not re-ask the model
        intent = old.intent.model_copy(update={"llm_memory": {**old.intent.llm_memory, **rec.intent.llm_memory}})
        change = ChangeRequest(message=message, operations=[], interpreter="restore")
        state = self._run_graph(rec, self.store.source_data(pid), intent, on_event)
        rev = self._commit(rec, state, intent, change, current)
        text = f"Restored the design of revision {letter} as revision {rev.revision} — {rev.diff_summary} " + self._validation_sentence(rev)
        rec.chat.append(
            ChatMessage(role="assistant", text=text, revision=rev.revision, data={"events": rev.events, "llm": self._llm_summary(calls)})
        )
        self.store.save(rec)
        return rev

    # ---- review ------------------------------------------------------------------------------
    def approve(self, pid: str, letter: str, reviewer: str = "reviewer") -> Revision:
        rev = self.store.revision(pid, letter)
        if not rev.validation.passed:
            raise ValueError("A revision with validation errors cannot be approved.")
        rev.status = "approved"
        rev.approved_by = reviewer
        rev.approved_at = now_iso()
        svg = render_svg(rev.model, self.standards, revision=rev.revision, status=f"APPROVED BY {reviewer.upper()}")
        self.store.update_revision(pid, rev, svg)
        rec = self.store.get(pid)
        rec.chat.append(ChatMessage(role="assistant", text=f"Revision {letter} approved by {reviewer}.", revision=letter))
        self.store.save(rec)
        return rev

    def dxf(self, pid: str, letter: str) -> tuple[str, bytes]:
        """(filename, DXF bytes) of a revision's clean drawing — no change highlighting."""
        from .engine.dxf_export import svg_to_dxf

        rec = self.store.get(pid)
        name = f"{rec.info.drawing_number or pid}_rev{letter}.dxf"
        return name, svg_to_dxf(self.svg(pid, letter, highlight_changes=False))

    def svg(self, pid: str, letter: str, highlight_changes: bool = True) -> str:
        rev = self.store.revision(pid, letter)
        hl = rev.diff.changed_tags() if (rev.diff and highlight_changes) else set()
        status = f"APPROVED BY {rev.approved_by.upper()}" if rev.status == "approved" and rev.approved_by else "DRAFT — AI GENERATED FOR REVIEW"
        return render_svg(rev.model, self.standards, highlight=hl, revision=rev.revision, status=status)
