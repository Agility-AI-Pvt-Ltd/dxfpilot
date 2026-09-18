# CadPilot

An autonomous first-draft P&ID engineering system. CadPilot takes a mass balance, equipment/design
data, the user's request and configurable engineering standards, and produces a complete, validated,
editable P&ID draft. The engineer reviews it and corrects it in conversation; every correction
becomes a structured change to the engineering model, is re-validated, and produces a new revision.

```
Excel + request ─► Primary agent ─► Process ∥ Equipment ─► Topology ─► Instrumentation
                                            │
                     merge ─► deterministic validator ─► (replan | commit) ─► layout ─► SVG P&ID
                                                                                 │
                              human review ◄─────────────────────────────────────┘
                                  │ "The separator should be before the pasteurizer"
                                  ▼
                  interpret → structured ChangeOp → design intent → regenerate → validate → Rev B
```

## What works today

- **Excel ingestion** of the NDDB-style dummy workbooks (`data/`): mass balance, design criteria and
  the 295-row equipment estimate. Tables are located by header text, not fixed cells.
- **Autonomous generation** (LangGraph): the primary agent plans and delegates; the Process and
  Equipment agents run in parallel; the primary agent reconciles them; the Topology and
  Instrumentation agents follow; the merge engine builds one engineering model.
- **Deterministic validation before the human sees anything**: structural (unique tags, references,
  orphans), topology (ports, inlet/outlet, inlet→outlet paths, islands, cycles), engineering rules
  (every instrumentation and line rule re-checked independently, process-order constraints) and data
  (mass balance vs. capacity hours, storage vs. intake, undersized units, line velocity, Excel ↔
  model). Each issue names the agent that owns the fix, and failures route a replan.
- **Deterministic layout and SVG rendering** from a controlled symbol library, with area boundaries,
  line numbers, valves, instrument bubbles, control-loop signal lines and a title block.
- **Conversational correction**: reorder stages, change capacities, change train counts, add/remove
  bypasses, add instruments, remove items (recorded as reviewer rule waivers), move items on the
  sheet, and "why is X required?" answered from provenance.
- **Revisions**: A, B, C… each with model snapshot, validation, agent run log, the structured change
  request, and a tag-level diff. Tags are stable across regenerations, so a revision shows only what
  actually changed (clouded in red on the drawing). Approval is blocked while validation has errors.
- **Web workspace**: chat on the left; P&ID with zoom, pan, select and inspect on the right; tabs for
  validation, changes, equipment/line/instrument lists and agent decisions.

Scope is the MVP slice: milk reception → raw milk storage → pumping → pasteurization.
For the dummy data that is 19 equipment items, 26 lines, 45 instruments, 10 control loops, 24 valves.

## Run it

PostgreSQL (Docker):

```bash
docker-compose up -d db
```

Backend (Python 3.11+, [uv](https://docs.astral.sh/uv/)). Copy the env file first; it contains
`DATABASE_URL` for the Docker database. The schema is created automatically on startup.

```bash
cp backend/.env.example backend/.env
cd backend && uv sync && uv run uvicorn cadpilot.api.main:app --port 8000
```

Frontend (Node 20+):

```bash
cd frontend && npm install && npm run dev
```

Open http://localhost:3000, upload the mass balance and the equipment/design-data workbooks (or
choose **Use the bundled dairy dummy data**). Tests run every storage test against both the file
store and PostgreSQL (in a throwaway schema; skipped if the database is not reachable):

```bash
cd backend && uv run pytest
```

### Storage

With `DATABASE_URL` set, everything is stored in PostgreSQL:

| Table | Contents |
|---|---|
| `projects` | project info, design intent (all accepted corrections), tag registry |
| `source_files` | the uploaded workbooks (bytes + sha256), one per role |
| `source_data` | what was parsed from them |
| `revisions` | every version: engineering model, validation, agent run log, change request, diff, SVG, approval, plus count columns for querying |
| `chat_messages` | the review conversation |
| `corrections` | structured reviewer corrections (JSONB, GIN-indexed) for later rule mining |

Without `DATABASE_URL` the API falls back to a file store in `backend/storage`. To copy projects
from there into PostgreSQL: `uv run python -m cadpilot.import_files` (safe to re-run).

### LLM reasoning (optional)

Without credentials every agent runs on its deterministic rules, and the whole flow works offline.
To enable LLM reasoning, copy the example env file and add your key:

```bash
cp backend/.env.example backend/.env
```

Then set `OPENAI_API_KEY`, and optionally `OPENAI_MODEL` (default `gpt-4.1`) and `OPENAI_BASE_URL`
(any OpenAI-compatible endpoint; empty means OpenAI), and restart the API. With the key empty, every
agent runs on its rules. `GET /api/health` shows whether the LLM is on and which model it uses.

The LLM is used where reasoning helps: interpreting the request (process agent), mapping design-data
rows to process stages (equipment agent), and turning free-form corrections into structured change
operations. Its output is always a schema-validated pydantic object; on any failure the agent falls
back to its rules.

## Layout

```
backend/cadpilot/
  model/        engineering model, design intent + ChangeOps, agent proposals, validation issues
  standards/    YAML standards layer: process templates, tagging, line rules, instrumentation
                rules, validation limits, layout, symbol library (overlay with CADPILOT_STANDARDS_OVERLAYS)
  ingest/       Excel → SourceData
  agents/       primary, process, equipment, topology, instrumentation, correction interpreter, LLM boundary
  engine/       tags (stable registry), sizing, merge, validate, layout, render, revisions (diff), apply (ChangeOps)
  graph/        LangGraph generation workflow
  service.py    generation, correction, revisions, approval
  store.py      store interface + file fallback;  pg_store.py  PostgreSQL store (schema migrations)
  api/          FastAPI + SSE progress streaming
frontend/       Next.js review workspace
data/           the two dummy workbooks
docs/           architecture notes
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design decisions and the roadmap.
