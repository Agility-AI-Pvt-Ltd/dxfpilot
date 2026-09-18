"""Application service: generation, conversational correction, revisions, approval."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .agents.interpreter import interpret, interpret_rules
from .agents.llm import LLMCall, record_calls
from .engine.apply import apply_ops
from .engine.render import render_svg
from .engine.revisions import diff_models, next_revision
from .graph.generation import build_generation_graph, initial_state
from .ingest.excel import load_workbooks
from .model.intent import ChangeRequest, DesignIntent, Explain
from .standards import get_standards
from .store import ChatMessage, ProjectRecord, ProjectStore, Revision, get_store, now_iso

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

    def set_sources(self, pid: str, files: dict[str, tuple[str, bytes]]) -> ProjectRecord:
        """files: role -> (original filename, bytes). Replaces the workbook for each given role."""
        self.store.put_sources(pid, files)
        data = load_workbooks(list(self.store.source_files(pid).values()))
        self.store.save_source_data(pid, data)
        rec = self.store.get(pid)
        if data.plant_title:
            rec.info.plant = data.plant_title
        self.store.save(rec)
        return rec

    # ---- autonomous generation ----------------------------------------------------------------
    def run_generation(self, pid: str, request: str, on_event=None) -> Revision:
        with record_calls() as calls:
            rev = None
            try:
                rev = self._run_generation(pid, request, on_event, calls)
                return rev
            finally:
                self.store.record_llm_calls(pid, rev.revision if rev else None, calls)

    @staticmethod
    def _llm_summary(calls: list[LLMCall]) -> list[dict[str, Any]]:
        return [
            {"purpose": c.purpose, "outcome": c.outcome, "model": c.served_model or c.model, "ms": c.latency_ms}
            for c in calls
        ]

    def _run_generation(self, pid: str, request: str, on_event, calls: list[LLMCall]) -> Revision:
        rec = self.store.get(pid)
        source = self.store.source_data(pid)
        if source is None:
            raise ValueError("Upload the mass balance and design data first.")
        intent = rec.intent.model_copy(update={"request": request or rec.intent.request})
        rec.chat.append(ChatMessage(role="user", text=request or "Generate the P&ID"))
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
        for update in self.graph.stream(state, stream_mode="updates"):
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
    def chat(self, pid: str, message: str, selected: str | None = None, on_event=None) -> dict[str, Any]:
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

    def restore(self, pid: str, letter: str, on_event=None) -> Revision:
        """Bring back an earlier revision's design as a new revision (history is never rewritten)."""
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
