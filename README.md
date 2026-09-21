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

**Everything in Docker** (the same stack the EC2 deployment runs):

```bash
cp .env.example .env        # set POSTGRES_PASSWORD and the OPENAI_* values
docker compose up -d --build
```

Open http://localhost/ (the gateway serves the web app and `/api`).

**Local development** (database in Docker, API and web on your machine with reload):

```bash
docker compose up -d db
cp backend/.env.example backend/.env
cd backend && uv sync && uv run uvicorn cadpilot.api.main:app --port 8000 --reload
cd frontend && npm install && npm run dev      # http://localhost:3000
```

Tests (every storage test runs against the file store and PostgreSQL):

```bash
cd backend && uv run pytest
```

**Deployment:** GitHub Actions tests every pull request and deploys `main` to EC2 — see
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

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

### Seeing what came from the LLM and what came from rules

- In the app: every chat reply carries a badge (e.g. `interpreted by llm · openai/gpt-4.1-mini · 2.1 s`
  or `rules (model reply unusable)`), and the **Decisions** tab lists every model call with tokens.
- In **LangSmith**: set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` (plus optionally
  `LANGSMITH_PROJECT`, and `LANGSMITH_ENDPOINT` for the EU region) in `backend/.env` locally or in the
  server `.env`, and restart the API. Each draft, correction and restore becomes one trace:

  ```
  cadpilot: review message                         project_id=…
    correction_interpreter        decision_source=llm
      llm_call                    decision_source=llm  served_model=openai/gpt-4.1-mini
        ChatOpenAI                (prompt, reply, tokens)
    LangGraph
      process_agent               decision_source=llm      (llm_call → cached)
      equipment_agent             decision_source=rules (fallback: model reply unusable)
      topology_agent              decision_source=rules
      instrumentation_agent       decision_source=rules
      validate · layout · render
  ```

  Filter by the tags `llm`, `rules` or `cached`, or by metadata `decision_source`. Note that traces
  contain the prompts, i.e. your design-data rows and requests.

The LLM is used where reasoning helps: interpreting the request (process agent), mapping design-data
rows to process stages (equipment agent), and turning free-form corrections into structured change
operations. Its output is always a schema-validated pydantic object; on any failure the agent falls
back to its rules.

## Engineering modules

A drawing is composed of plant building blocks. Each module is one YAML file in
`backend/cadpilot/standards/modules/` and carries everything the deterministic engine needs for that
section of the plant:

| | Module | Chain |
|---|---|---|
| 1 | Milk reception | tankers → strainer → de-aeration → unloading pump → chiller → silos |
| 2 | Milk pasteurization | balance tank → feed pump → pasteurizer → holding tube → cooling |
| 3 | CIP station | CIP return → lye / acid / hot water / recovered water tanks → forward pump → heater; chemical dosing into lye and acid tanks |
| 4 | Steam | boilers → PRV station → one take-off per steam user; condensate tank → feed pumps → boilers |
| 5 | Hot water | return → buffer tank → pumps → steam-heated generator → one supply per hot-water user |
| 6 | Chilled water | return → ice-bank tank → pumps → water chiller → one supply per chilled-water user |
| 7 | Curd | milk → heater → incubation tanks (culture dosing) → breaking pump → cooler → packing |
| 8 | Ghee | butter → melter → ghee boilers → clarifier → settling tanks → packing |
| 9 | Cream | separators → skim back; cream → buffer tank → cream pasteurizer → cooler → ripening tanks |
| 10 | Milk packaging | re-chiller → filler overhead tanks → pouch fillers |

A module file declares:

- `stages` in flow order, each with `equipment_type`, `group` (parallel trains), `match` (Excel keywords) and
  `default_capacity`. Side chains use `branch` with `feeds` (into another stage, on its auxiliary inlet) or
  `source` (from a stage's second outlet).
- `groups`: how many parallel units (from the design data via `lead_stage`, or fixed `train_labels`).
- `instrumentation_rules` and `line_rules`: the module's own engineering rules, same format as the shared
  files. Line rules can filter by `from_type`, `to_type`, `service`, `not_service` and `header_branch`.
- `symbols` and `tag_prefixes` for its equipment types (`same_as: vessel_tank` reuses a shared symbol).
- Interfaces: `connects_to` links a battery limit to a stage of another module (flows carry across, and the
  label names the other area); `utility_users: steam` creates one take-off per consumer whose
  `attributes.utility` names that utility.

Choose modules on the new-draft screen or under **Design data**, or name them in the request ("draw the CIP
station and the steam system", "the whole plant"). With no module selected, the classic single-sheet
reception → pasteurization line is drawn. Stage and group ids must be unique across modules; the loader
refuses clashes. Every module is tested alone and in the whole-plant composition
(`backend/tests/test_modules.py`).

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
