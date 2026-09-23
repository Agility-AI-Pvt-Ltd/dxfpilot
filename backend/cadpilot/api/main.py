"""HTTP API. Generation and chat stream progress as Server-Sent Events."""

from __future__ import annotations

import json
import os
import queue
import threading
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from ..ingest.excel import TableOverride
from ..integrations import crm
from ..service import CadPilot
from ..standards import MODULE_PREFIX, get_standards, module_ids

@asynccontextmanager
async def lifespan(_app: FastAPI):
    pilot()  # open the store (runs DB migrations) at startup, not on the first request
    yield
    if _pilot is not None:
        _pilot.store.close()


app = FastAPI(title="CadPilot API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    # CADPILOT_CORS_ORIGINS: extra browser origins (comma-separated), e.g. the CRM's web address
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"]
    + [o.strip().rstrip("/") for o in os.environ.get("CADPILOT_CORS_ORIGINS", "").split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

_pilot: CadPilot | None = None
_locks: dict[str, threading.Lock] = {}


def pilot() -> CadPilot:
    global _pilot
    if _pilot is None:
        _pilot = CadPilot()
    return _pilot


def _lock(pid: str) -> threading.Lock:
    return _locks.setdefault(pid, threading.Lock())


class CreateProject(BaseModel):
    name: str
    demo_data: bool = False


class RenameProject(BaseModel):
    name: str


class GenerateRequest(BaseModel):
    request: str = "Generate the P&ID for milk reception to pasteurization"
    engine: Literal["rules", "llm_planner"] | None = None  # None = keep the project's current engine
    modules: list[str] | None = None  # engineering modules to draw; None = keep the project's scope
    detect_modules: bool = False  # read the workbooks and draw the plant sections they contain


class ChatRequest(BaseModel):
    message: str
    selected: str | None = None


def _project_view(pid: str) -> dict[str, Any]:
    src = pilot().source_data(pid)  # re-reads data parsed by an older reader
    rec = pilot().store.get(pid)
    return {
        "id": rec.info.id,
        "name": rec.info.name,
        "plant": rec.info.plant,
        "drawing_number": rec.info.drawing_number,
        "sources": rec.sources,
        "source_summary": None
        if src is None
        else {
            "equipment_rows": len(src.equipment_rows),
            "mass_balance_inputs": [e.model_dump() for e in src.mass_balance_inputs],
            "mass_balance_outputs": len(src.mass_balance_outputs),
            "design_criteria": src.design_criteria[:12],
            "plant_title": src.plant_title,
            "tables": src.files,
            "detections": [d.model_dump() for d in src.detections],
            "flagged_rows": [r.model_dump() for r in src.flagged_rows()[:60]],
            "ai_read_rows": [r.model_dump() for r in src.equipment_rows if r.read_by == "llm"][:60],
            "warnings": src.warnings,
        },
        "revisions": rec.revisions,
        "current": rec.current,
        "chat": [m.model_dump() for m in rec.chat],
        "intent": rec.intent.model_dump(),
        "modules": module_ids(rec.intent.template) if rec.intent.template.startswith(MODULE_PREFIX) else [],
        "table_overrides": [o.model_dump() for o in rec.table_overrides],
        "integration": None
        if rec.integration is None
        else {
            "source": rec.integration.source,
            "external_id": rec.integration.external_id,
            "return_url": rec.integration.return_url,
            "request": rec.integration.request,
            "has_callback": bool(rec.integration.callback_url),
            "deliveries": [d.model_dump() for d in rec.integration.deliveries],
        },
    }


def _get(fn, *args):
    try:
        return fn(*args)
    except KeyError as exc:
        raise HTTPException(404, f"Not found: {exc}") from exc


@app.get("/api/modules")
def list_modules() -> list[dict[str, Any]]:
    """The engineering modules a drawing can be composed of (building blocks of a dairy plant)."""
    std = get_standards()
    out = []
    for m in sorted(std.modules.values(), key=lambda m: m.get("order", 99)):
        out.append({
            "id": m["id"], "name": m["name"], "area": str(m["area"]), "summary": m.get("summary", ""),
            "utility": m.get("utility"),
            "equipment": [s["name"] for s in m["stages"] if s["kind"] == "equipment"],
            "rules": len(m.get("instrumentation_rules", [])) + len(m.get("line_rules", [])),
        })
    return out


@app.post("/api/projects/{pid}/scope")
def detect_scope(pid: str) -> dict[str, Any]:
    """Read the workbooks: which plant sections they contain, with the rows as evidence."""
    try:
        return pilot().detect_scope(pid).model_dump()
    except KeyError as e:
        raise HTTPException(404, "Project not found") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/health")
def health() -> dict[str, Any]:
    from ..agents.llm import get_llm

    llm = get_llm()
    store = type(pilot().store).__name__
    return {"ok": True, "llm": llm.enabled, "model": llm.model if llm.enabled else None, "store": store}


@app.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    return [s.model_dump() for s in pilot().store.list_projects()]


@app.post("/api/projects")
def create_project(body: CreateProject) -> dict[str, Any]:
    rec = pilot().create_project(body.name, body.demo_data)
    return _project_view(rec.info.id)


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict[str, Any]:
    return _get(_project_view, pid)


@app.patch("/api/projects/{pid}")
def rename_project(pid: str, body: RenameProject) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "The project name cannot be empty")
    with _lock(pid):
        rec = _get(pilot().store.get, pid)
        rec.info.name = name
        pilot().store.save(rec)
    return _project_view(pid)


@app.post("/api/projects/{pid}/sources")
async def upload_sources(
    pid: str, mass_balance: UploadFile | None = None, design_data: UploadFile | None = None
) -> dict[str, Any]:
    """Upload the mass balance and/or the equipment & design-criteria workbook. Each replaces
    the previous file of the same role."""
    payload: dict[str, tuple[str, bytes]] = {}
    for role, f in (("mass_balance", mass_balance), ("design_data", design_data)):
        if f is None:
            continue
        if not (f.filename or "").lower().endswith(".xlsx"):
            raise HTTPException(400, f"{f.filename}: only .xlsx workbooks are supported")
        payload[role] = (f.filename or f"{role}.xlsx", await f.read())
    if not payload:
        raise HTTPException(400, "Send mass_balance and/or design_data as .xlsx files")
    try:
        _get(pilot().set_sources, pid, payload)
    except Exception as exc:  # unreadable workbook
        raise HTTPException(400, f"Could not read the workbook: {exc}") from exc
    return _project_view(pid)


def _sse(run) -> StreamingResponse:
    """Run `run(on_event)` in a worker thread and stream its events as SSE."""
    q: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            result = run(lambda ev: q.put(("event", ev)))
            q.put(("result", result))
        except Exception as exc:  # surfaced to the client as an error event
            q.put(("error", {"detail": str(exc)}))
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while (item := q.get()) is not None:
            kind, data = item
            yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/projects/{pid}/generate")
def generate(pid: str, body: GenerateRequest) -> StreamingResponse:
    _get(pilot().store.get, pid)

    def run(on_event):
        with _lock(pid):
            rev = pilot().run_generation(pid, body.request, on_event, body.engine, body.modules, body.detect_modules)
        return {"revision": rev.revision, "status": rev.status, "reply": pilot().store.get(pid).chat[-1].text}

    return _sse(run)


@app.post("/api/projects/{pid}/chat")
def chat(pid: str, body: ChatRequest) -> StreamingResponse:
    _get(pilot().store.get, pid)

    def run(on_event):
        with _lock(pid):
            return pilot().chat(pid, body.message, body.selected, on_event)

    return _sse(run)


@app.get("/api/projects/{pid}/llm-calls")
def llm_calls(pid: str) -> list[dict[str, Any]]:
    """Audit log: every LLM call made for this project, oldest first."""
    _get(pilot().store.get, pid)
    return pilot().store.llm_calls(pid)


@app.get("/api/projects/{pid}/sources/sheets")
def source_sheets(pid: str) -> list[dict[str, Any]]:
    """Every sheet of the uploaded workbooks, as text, for the column-mapping editor."""
    return _get(pilot().sheets, pid)


@app.post("/api/projects/{pid}/sources/mapping/preview")
def preview_mapping(pid: str, body: TableOverride) -> dict[str, Any]:
    """What a proposed mapping would read, without saving it."""
    return _get(pilot().preview_mapping, pid, body)


@app.put("/api/projects/{pid}/sources/mapping")
def set_mapping(pid: str, body: TableOverride) -> dict[str, Any]:
    _get(pilot().set_mapping, pid, body)
    return _project_view(pid)


@app.delete("/api/projects/{pid}/sources/mapping/{kind}")
def clear_mapping(pid: str, kind: str) -> dict[str, Any]:
    if kind not in ("equipment_list", "mass_balance"):
        raise HTTPException(400, "kind must be equipment_list or mass_balance")
    _get(pilot().clear_mapping, pid, kind)
    return _project_view(pid)


@app.get("/api/projects/{pid}/revisions")
def list_revisions(pid: str) -> list[dict[str, Any]]:
    """Version history, oldest first."""
    return [r.model_dump() for r in _get(pilot().store.list_revisions, pid)]


@app.get("/api/projects/{pid}/revisions/{rev}")
def get_revision(pid: str, rev: str) -> dict[str, Any]:
    r = _get(pilot().store.revision, pid, rev)
    data = json.loads(r.model_dump_json(by_alias=True))
    data["validation"]["summary"] = r.validation.summary()
    data["counts"] = r.model.counts()
    data["changed_tags"] = sorted(r.diff.changed_tags()) if r.diff else []
    return data


@app.get("/api/projects/{pid}/revisions/{rev}/svg")
def get_svg(pid: str, rev: str, highlight: bool = True) -> Response:
    svg = _get(pilot().svg, pid, rev, highlight)
    return Response(svg, media_type="image/svg+xml")


@app.get("/api/projects/{pid}/revisions/{rev}/dxf")
def download_dxf(pid: str, rev: str) -> Response:
    name, data = _get(pilot().dxf, pid, rev)
    return Response(data, media_type="application/dxf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/projects/{pid}/revisions/{rev}/model.json")
def download_model(pid: str, rev: str) -> Response:
    r = _get(pilot().store.revision, pid, rev)
    return Response(
        r.model.model_dump_json(indent=2, by_alias=True),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{pid}-rev{rev}.json"'},
    )


@app.post("/api/projects/{pid}/revisions/{rev}/restore")
def restore(pid: str, rev: str) -> StreamingResponse:
    _get(pilot().store.revision, pid, rev)

    def run(on_event):
        with _lock(pid):
            r = pilot().restore(pid, rev, on_event)
        return {"revision": r.revision, "status": r.status, "reply": pilot().store.get(pid).chat[-1].text}

    return _sse(run)


@app.post("/api/projects/{pid}/revisions/{rev}/approve")
def approve(pid: str, rev: str) -> dict[str, Any]:
    try:
        r = _get(pilot().approve, pid, rev)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    delivering = _deliver_in_background(pid, r.revision)
    return {"revision": r.revision, "status": r.status, "delivering": delivering}


# ---- CRM integration (see cadpilot/integrations/crm.py and docs/CRM_INTEGRATION.md) ----------

def _deliver_in_background(pid: str, letter: str) -> bool:
    """Send an approved drawing to the CRM that created the project. False if there is nowhere to send it."""
    rec = pilot().store.get(pid)
    if not (rec.integration and rec.integration.callback_url):
        return False

    def worker() -> None:
        try:
            d = crm.send(pilot(), pid, letter)  # network I/O outside the project lock
        except Exception as exc:  # e.g. the drawing could not be exported
            d = crm.Delivery(revision=letter, detail=f"{type(exc).__name__}: {exc}"[:300])
        with _lock(pid):
            crm.record(pilot(), pid, d)

    threading.Thread(target=worker, daemon=True).start()
    return True


def _crm_auth(x_api_key: str | None, authorization: str | None) -> None:
    key = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None)
    try:
        crm.check_key(key)
    except crm.IntegrationDisabled as exc:
        raise HTTPException(503, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc


def _crm_project(external_id: str) -> str:
    pid = pilot().store.find_external(crm.SOURCE, external_id)
    if pid is None:
        raise HTTPException(404, f"No project for CRM record {external_id!r}")
    return pid


@app.post("/api/integrations/crm/projects")
async def crm_create_project(
    external_id: str = Form(...),
    name: str = Form(""),
    callback_url: str | None = Form(None),
    return_url: str | None = Form(None),
    request: str = Form(""),
    mass_balance: UploadFile | None = None,
    design_data: UploadFile | None = None,
    x_api_key: str | None = Header(None),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """The CRM hands over a record's two workbooks. Idempotent on external_id: sending the same
    record again updates its project (new files replace the old ones) instead of creating another."""
    _crm_auth(x_api_key, authorization)
    files: dict[str, tuple[str, bytes]] = {}
    for role, f in (("mass_balance", mass_balance), ("design_data", design_data)):
        if f is None:
            continue
        if not (f.filename or "").lower().endswith(".xlsx"):
            raise HTTPException(400, f"{f.filename}: only .xlsx workbooks are supported")
        files[role] = (f.filename or f"{role}.xlsx", await f.read())
    existing = pilot().store.find_external(crm.SOURCE, external_id.strip())
    if existing is None and len(files) < 2:
        raise HTTPException(400, "A new project needs both mass_balance and design_data (.xlsx)")
    try:
        with _lock(existing or f"crm:{external_id}"):
            pid, created = crm.intake(pilot(), external_id, name, files, callback_url, return_url, request)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # unreadable workbook
        raise HTTPException(400, f"Could not read the workbook: {exc}") from exc
    out = crm.status(pilot(), pid)
    out["created"] = created
    out["status_url"] = f"{crm.public_url()}/api/integrations/crm/projects/{out['external_id']}"
    return out


@app.get("/api/integrations/crm/projects/{external_id}")
def crm_project_status(external_id: str, x_api_key: str | None = Header(None), authorization: str | None = Header(None)) -> dict[str, Any]:
    """Where the record's drawing is: draft_pending → in_review → approved (with DXF/SVG links)."""
    _crm_auth(x_api_key, authorization)
    return crm.status(pilot(), _crm_project(external_id))


@app.get("/api/integrations/crm/projects/{external_id}/dxf")
def crm_download_dxf(external_id: str, x_api_key: str | None = Header(None), authorization: str | None = Header(None)) -> Response:
    """The latest approved drawing as DXF (409 while nothing is approved)."""
    _crm_auth(x_api_key, authorization)
    pid = _crm_project(external_id)
    letter = crm.approved_revision(pilot(), pid)
    if letter is None:
        raise HTTPException(409, "No approved revision yet")
    name, data = pilot().dxf(pid, letter)
    return Response(data, media_type="application/dxf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/integrations/crm/projects/{external_id}/redeliver")
def crm_redeliver(external_id: str, x_api_key: str | None = Header(None), authorization: str | None = Header(None)) -> dict[str, Any]:
    _crm_auth(x_api_key, authorization)
    return _redeliver(_crm_project(external_id))


@app.post("/api/projects/{pid}/integration/redeliver")
def redeliver(pid: str) -> dict[str, Any]:
    """Workspace button: send the latest approved drawing to the CRM again."""
    _get(pilot().store.get, pid)
    return _redeliver(pid)


def _redeliver(pid: str) -> dict[str, Any]:
    letter = crm.approved_revision(pilot(), pid)
    if letter is None:
        raise HTTPException(409, "No approved revision yet")
    if not _deliver_in_background(pid, letter):
        raise HTTPException(409, "This project has no CRM callback URL")
    return {"revision": letter, "delivering": True}


@app.get("/api/integrations/crm/files/{pid}/{name}")
def crm_file(pid: str, name: str, token: str = "") -> Response:
    """Signed link to an approved drawing, for the CRM's web page (no API key in the browser)."""
    letter, _, kind = name.rpartition(".")
    if kind not in ("dxf", "svg") or not crm.check_file_token(pid, letter, kind, token):
        raise HTTPException(404, "Not found")
    rev = _get(pilot().store.revision, pid, letter)
    if rev.status != "approved":
        raise HTTPException(404, "Not found")
    if kind == "svg":
        return Response(pilot().svg(pid, letter, False), media_type="image/svg+xml",
                        headers={"Content-Disposition": f'inline; filename="{pid}_rev{letter}.svg"'})
    fname, data = pilot().dxf(pid, letter)
    return Response(data, media_type="application/dxf", headers={"Content-Disposition": f'attachment; filename="{fname}"'})
