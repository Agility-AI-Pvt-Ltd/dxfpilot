export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type ChatMessage = {
  role: "user" | "assistant";
  text: string;
  at: string;
  revision?: string | null;
  data?: { events?: ProgressEvent[]; selected?: string; interpreter?: string; llm?: LLMCallSummary[] };
};

export type Project = {
  id: string;
  name: string;
  plant: string;
  drawing_number: string;
  sources: Partial<Record<SourceRole, string>>;
  source_summary: SourceSummary | null;
  revisions: string[];
  current: string | null;
  chat: ChatMessage[];
  table_overrides: TableOverride[];
  intent: { engine?: Engine; scope_detection?: ScopeDetection | null };
  /** Engineering modules this drawing is composed of; empty = the classic reception → pasteurization line */
  modules: string[];
  /** Set when the CRM created this project; approved drawings are sent back to it */
  integration: Integration | null;
};

export type Delivery = { revision: string; at: string; ok: boolean; http_status: number | null; detail: string };
export type Integration = {
  source: string;
  external_id: string;
  return_url: string | null;
  request: string;
  has_callback: boolean;
  deliveries: Delivery[];
};

/** What the scope agent found in the workbooks: plant sections with the rows as evidence */
export type ScopeFinding = {
  id: string;
  name: string;
  status: "found" | "partial" | "not_found";
  found_by: "rows" | "ai" | "none";
  matched: number;
  total: number;
  evidence: { ref: string; text: string; capacity: string | null; qty: number | null; stage: string | null }[];
  note: string;
};
export type ScopeDetection = { modules: ScopeFinding[]; selected: string[]; used_llm: boolean };

export type EngineeringModule = {
  id: string;
  name: string;
  area: string;
  summary: string;
  utility: string | null;
  equipment: string[];
  rules: number;
};

export type Engine = "rules" | "llm_planner";
export const ENGINE_LABEL: Record<Engine, string> = { rules: "Rules engine (recommended)", llm_planner: "LLM planner (experimental)" };

export type PlannerAttempt = {
  attempt: number;
  plan: string;
  changes?: string;
  errors: string[];
  warnings: number;
  kept?: boolean;
  patch_problems?: string[];
};

export type TableKind = "equipment_list" | "mass_balance";

export type TableOverride = {
  file: string;
  sheet: string;
  kind: TableKind;
  header_row: number | null;
  first_data_row: number | null;
  columns: Record<string, string>;
  capacity_unit: string | null;
};

export type SheetGrid = { file: string; sheet: string; role: SourceRole | null; columns: string[]; rows: string[][] };

export type SourceRow = {
  ref: string;
  section: string;
  description: string;
  capacity_raw: string | null;
  capacity: Quantity | null;
  qty: number | null;
  qty_raw: string | null;
  uom: string | null;
  note: string;
  flags: string[];
  read_by: "rules" | "llm";
};

export type MappingPreview = {
  total: number;
  with_quantity?: number;
  with_capacity?: number;
  flagged?: number;
  intake_litres?: number | null;
  rows: Record<string, unknown>[];
  warnings: string[];
};

export type SourceRole = "mass_balance" | "design_data";

export type SourceSummary = {
  equipment_rows: number;
  mass_balance_inputs: { name: string; litres: number | null; fat: number | null; snf: number | null }[];
  mass_balance_outputs: number;
  design_criteria: string[];
  plant_title: string;
  tables: string[];
  detections: TableDetection[];
  flagged_rows: SourceRow[];
  ai_read_rows: SourceRow[];
  warnings: string[];
};

export type TableDetection = {
  file: string;
  sheet: string;
  kind: "equipment_list" | "mass_balance" | "design_criteria";
  method: "headers" | "llm" | "content" | "manual";
  header_row: number | null;
  columns: Record<string, string>;
  rows: number;
};

/** What is still missing before a draft can be generated (empty = ready). */
export function missingSources(s: SourceSummary | null): string[] {
  const out: string[] = [];
  if (!s || !s.mass_balance_inputs.length) out.push("No mass balance table (Milk / in L / in Kg / Fat / SNF) was found.");
  if (!s || !s.equipment_rows) out.push("No equipment table (DESCRIPTION / CAPACITY / QTY) was found.");
  return out;
}

export type LLMCallSummary = { purpose: string; outcome: string; model: string; ms: number };

export type LLMCall = {
  purpose: string;
  model: string;
  served_model: string | null;
  outcome: string;
  latency_ms: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  detail: string;
  revision: string | null;
  at: string;
};

export type RevisionSummary = {
  revision: string;
  status: "ready_for_review" | "needs_attention" | "approved" | "superseded";
  created_at: string;
  cause: string;
  diff_summary: string;
  equipment: number;
  lines: number;
  instruments: number;
  errors: number;
  warnings: number;
  approved_by: string | null;
};

export type ProjectListItem = { id: string; name: string; drawing_number: string; current: string | null; created_at: string };

export type ProgressEvent = { step: string; detail: string; llm?: boolean; passed?: boolean };

export type Provenance = { agent: string; rule?: string | null; source?: string | null; rationale: string };
export type Quantity = { value: number; unit: string };

export type Equipment = {
  tag: string;
  type: string;
  name: string;
  stage: string;
  area: string;
  train: number | null;
  capacity: Quantity | null;
  attributes: Record<string, string | number | boolean>;
  provenance: Provenance;
};

export type InlineComponent = { tag: string; type: string; position: number; provenance: Provenance };

export type Line = {
  tag: string;
  from: { item: string; port: string };
  to: { item: string; port: string };
  service: string;
  kind: string;
  design_flow: Quantity | null;
  size_dn: number | null;
  spec: string;
  inline: InlineComponent[];
  provenance: Provenance;
};

export type Instrument = {
  tag: string;
  function: string;
  type: string;
  location: string;
  attached_to: { kind: string; ref: string; port?: string | null };
  loop: string | null;
  alarms: string[];
  provenance: Provenance;
};

export type Loop = {
  tag: string;
  measured_by: string;
  final_element: string;
  final_element_kind: string;
  setpoint: string;
  provenance: Provenance;
};

export type Issue = {
  layer: string;
  severity: "error" | "warning" | "info";
  code: string;
  message: string;
  refs: string[];
  owner: string | null;
  rule: string | null;
};

export type CategoryDiff = { added: string[]; removed: string[]; modified: Record<string, string[]>; unchanged: number };

export type Revision = {
  revision: string;
  created_at: string;
  status: "ready_for_review" | "needs_attention" | "approved" | "superseded";
  model: {
    project: { name: string; drawing_number: string };
    process: { name: string; stages: { id: string; name: string; group: string }[]; groups: Record<string, number>; basis: Record<string, string | number> };
    equipment: Equipment[];
    piping_nodes: { tag: string; kind: string; label: string; provenance: Provenance }[];
    lines: Line[];
    instruments: Instrument[];
    loops: Loop[];
    metadata: {
      decisions?: { agent: string; summary: string; detail: string }[];
      assumptions?: string[];
      warnings?: string[];
      engine?: Engine;
      planner?: { attempts: number; max_attempts: number; history: PlannerAttempt[]; best_attempt?: number; stop_reason?: string };
    };
  };
  validation: { issues: Issue[]; checks_run: Record<string, number>; summary: { passed: boolean; errors: number; warnings: number } };
  events: ProgressEvent[];
  change_request: { message: string; operations: Record<string, unknown>[]; interpreter: string } | null;
  diff: { equipment: CategoryDiff; lines: CategoryDiff; instruments: CategoryDiff; valves: CategoryDiff } | null;
  diff_summary: string;
  approved_by: string | null;
  counts: Record<string, number>;
  changed_tags: string[];
};

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => fetch(`${API}/api/health`).then(json<{ ok: boolean; llm: boolean; model: string | null }>),
  modules: () => fetch(`${API}/api/modules`).then(json<EngineeringModule[]>),
  projects: () => fetch(`${API}/api/projects`).then(json<ProjectListItem[]>),
  project: (id: string) => fetch(`${API}/api/projects/${id}`).then(json<Project>),
  detectScope: (id: string) => fetch(`${API}/api/projects/${id}/scope`, { method: "POST" }).then(json<ScopeDetection>),
  create: (name: string, demo_data: boolean) =>
    fetch(`${API}/api/projects`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, demo_data }),
    }).then(json<Project>),
  rename: (id: string, name: string) =>
    fetch(`${API}/api/projects/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(json<Project>),
  upload: (id: string, files: Partial<Record<SourceRole, File>>) => {
    const fd = new FormData();
    for (const [role, f] of Object.entries(files)) if (f) fd.append(role, f);
    return fetch(`${API}/api/projects/${id}/sources`, { method: "POST", body: fd }).then(json<Project>);
  },
  sheets: (id: string) => fetch(`${API}/api/projects/${id}/sources/sheets`).then(json<SheetGrid[]>),
  previewMapping: (id: string, ov: TableOverride) =>
    fetch(`${API}/api/projects/${id}/sources/mapping/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ov),
    }).then(json<MappingPreview>),
  setMapping: (id: string, ov: TableOverride) =>
    fetch(`${API}/api/projects/${id}/sources/mapping`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ov),
    }).then(json<Project>),
  clearMapping: (id: string, kind: TableKind) =>
    fetch(`${API}/api/projects/${id}/sources/mapping/${kind}`, { method: "DELETE" }).then(json<Project>),
  llmCalls: (id: string) => fetch(`${API}/api/projects/${id}/llm-calls`).then(json<LLMCall[]>),
  revisions: (id: string) => fetch(`${API}/api/projects/${id}/revisions`).then(json<RevisionSummary[]>),
  revision: (id: string, rev: string) => fetch(`${API}/api/projects/${id}/revisions/${rev}`).then(json<Revision>),
  svg: (id: string, rev: string, highlight = true) =>
    fetch(`${API}/api/projects/${id}/revisions/${rev}/svg?highlight=${highlight}`).then((r) => r.text()),
  approve: (id: string, rev: string) =>
    fetch(`${API}/api/projects/${id}/revisions/${rev}/approve`, { method: "POST" }).then(json<{ status: string; delivering: boolean }>),
  redeliver: (id: string) =>
    fetch(`${API}/api/projects/${id}/integration/redeliver`, { method: "POST" }).then(json<{ revision: string; delivering: boolean }>),
  modelUrl: (id: string, rev: string) => `${API}/api/projects/${id}/revisions/${rev}/model.json`,
  dxfUrl: (id: string, rev: string) => `${API}/api/projects/${id}/revisions/${rev}/dxf`,
  svgUrl: (id: string, rev: string) => `${API}/api/projects/${id}/revisions/${rev}/svg?highlight=false`,
};

/** POST that streams Server-Sent Events: progress events, then a result (or error). */
export async function streamPost(
  path: string,
  body: unknown,
  onEvent: (ev: ProgressEvent) => void,
): Promise<Record<string, unknown>> {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error(`Request failed (${res.status})`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: Record<string, unknown> = {};
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const kind = /^event: (.*)$/m.exec(chunk)?.[1];
      const data = /^data: (.*)$/m.exec(chunk)?.[1];
      if (!kind || !data) continue;
      const parsed = JSON.parse(data);
      if (kind === "event") onEvent(parsed);
      else if (kind === "result") result = parsed;
      else if (kind === "error") throw new Error(parsed.detail);
    }
  }
  return result;
}
