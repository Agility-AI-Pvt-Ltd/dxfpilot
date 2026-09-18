# CadPilot architecture

## Principle

CadPilot drafts; the human reviews and corrects. The drawing is never the source of truth. The
**engineering model** is, and it is itself a pure function of three inputs:

```
engineering model = f(source data, design intent, standards)
```

- **Source data**: parsed workbooks (`SourceData`).
- **Design intent**: the request plus every accepted reviewer correction (`DesignIntent`):
  stage order, disabled/enabled optional stages, train counts, equipment overrides, bypasses, added
  instruments, reviewer waivers, sheet offsets.
- **Standards**: the layered YAML standards (`standards/`).

A correction is a structured `ChangeOp` applied to the design intent, followed by a full
regeneration. Because tags come from a persisted identity-keyed registry (`engine/tags.py`),
regeneration keeps every unchanged object's tag; the revision diff therefore shows only real
changes. Corrections survive every later regeneration: they are persistent engineering memory, and
`ProjectRecord.corrections` keeps them as structured records for later rule mining.

## LLM vs. deterministic responsibilities

| Concern | Who |
|---|---|
| Interpret the request and scope; decide unwanted optional stages | Process agent (LLM, rules fallback) |
| Map design-data rows to process stages | Equipment agent (LLM, keyword fallback) |
| Interpret a reviewer's correction into ChangeOps | Interpreter (LLM, regex fallback) |
| Tags, counts, capacities from data | deterministic |
| Topology, headers, bypass tees, line flows, line sizing, piping valves | deterministic (topology agent) |
| Instruments, loops, final elements | deterministic rules (instrumentation agent) |
| Validation, layout, rendering, diff | deterministic |

The Topology and Instrumentation agents are rule engines today. They take `ctx.feedback` (the
validator's issues) so an LLM planner can be added behind the same proposal interface without
touching the rest of the graph.

## Generation graph (LangGraph)

```
START → plan → {process_agent ∥ equipment_agent} → reconcile → topology_agent
      → instrumentation_agent → merge → validate ─┬─ pass → finalize → layout → render → END
                                                  └─ fail → replan → (plan | topology | instrumentation)
```

Deviation from the original sketch: Topology and Instrumentation do not run in parallel with the
other two workers. Topology needs the reconciled equipment list (it connects tagged equipment in
process order), and instrumentation attaches to lines. The primary agent reconciles Process and
Equipment first (group sizes from data, stages out of scope dropped, reviewer overrides applied).

Replans route to the earliest agent that owns an error (max 2). If errors persist the model is still
committed, as `needs_attention`, and approval is blocked.

Human review is outside the graph (API-driven) rather than a LangGraph interrupt. That keeps the graph
stateless and avoids needing a checkpointer. A correction runs `interpret → apply → the same
generation graph → diff → commit`.

## Validation layers

`engine/validate.py`: structural, topology, engineering, data. The validator imports the rule
*definitions*, not the instrumentation agent's output, so a rule the agent skips is still caught. A
reviewer removing a rule-required item is recorded as a waiver: the validator reports it as `info`
and names the reason.

## Standards layer

`standards/*.yaml` is the international-style baseline, not a claim of compliance: ISA-5.1-style
letters, ISO 10628-2-inspired placeholder symbols, sanitary SS316L spec. Organisation or project
standards overlay it via `CADPILOT_STANDARDS_OVERLAYS=/path/nddb:/path/project`, deep-merged in
order. Replace `symbols.yaml` with the client's library; the renderer only reads ids, sizes, ports
and SVG fragments.

## Storage

`ProjectStore` (store.py) is the only persistence boundary. `PostgresStore` (pg_store.py) is used
whenever `DATABASE_URL` is set; `FileStore` is the no-database fallback. Postgres specifics:

- Schema migrations are versioned in `MIGRATIONS`, tracked in `schema_migrations`, and applied at
  startup under an advisory lock (safe with several API workers).
- A revision commit is one transaction: insert the revision, mark the previous one superseded, save
  the project (intent, tag registry, new chat messages, new corrections). The project row is locked
  `FOR UPDATE`, so concurrent commits to one project serialise.
- Chat messages and corrections are append-only.
- Large engineering documents (model, validation, diff) are JSONB; counts and statuses are real
  columns so revisions can be queried without unpacking JSON.

## Roadmap

1. **DEXPI 2.0 adapter** (`cadpilot/dexpi/`), kept isolated behind `EngineeringModel ↔ DEXPI`
   because DEXPI 2.0.1 and the DEXPI Profile are still moving. Map equipment/nozzles, piping
   network segments, instrumentation functions and loops.
2. **LLM topology/instrumentation planners** for requirements that the rules don't cover, still
   emitting the same proposal schemas.
3. **More templates** (cream, CIP, packing), with multiple sheets and off-page connectors.
4. **Redis workers** for long runs, authentication, multi-user review comments.
5. **Rule mining** from `corrections`: turn repeated reviewer changes into project/organisation rules
   (reviewed by a human, never auto-trained).
6. **DXF/DWG export** from DEXPI for AutoCAD / Plant 3D.
