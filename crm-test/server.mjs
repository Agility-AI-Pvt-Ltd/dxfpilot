// Test CRM for the CadPilot integration — Node 18+, no dependencies.
//
//   CADPILOT_URL=http://localhost:8000 CADPILOT_CRM_API_KEY=dev-key node server.mjs
//   open http://localhost:4000
//
// The page (public/) never sees the API key: it talks to this server, which talks to CadPilot,
// exactly like a real CRM backend. This server also receives CadPilot's approval webhook.

import { createServer } from "node:http";
import { createHmac, timingSafeEqual } from "node:crypto";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join, extname, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 4000);
const CADPILOT_URL = (process.env.CADPILOT_URL || "http://localhost:8000").replace(/\/$/, "");
const API_KEY = process.env.CADPILOT_CRM_API_KEY || "";
const SECRET = process.env.CADPILOT_CRM_WEBHOOK_SECRET || API_KEY;
// Address CadPilot uses to reach this server's webhook. Must be reachable FROM CadPilot
// (localhost works only when CadPilot runs on the same machine).
const CALLBACK_BASE = (process.env.CALLBACK_BASE || `http://localhost:${PORT}`).replace(/\/$/, "");
const PUBLIC_BASE = (process.env.PUBLIC_BASE || CALLBACK_BASE).replace(/\/$/, "");
const DATA = join(HERE, "data");
const DB = join(DATA, "records.json");

await mkdir(join(DATA, "inbox"), { recursive: true });
const records = existsSync(DB) ? JSON.parse(await readFile(DB, "utf8")) : {};
const save = () => writeFile(DB, JSON.stringify(records, null, 1));
const log = (...a) => console.log(new Date().toLocaleTimeString(), ...a);

// ---- calls to CadPilot (server to server, with the API key) --------------------------------------

async function cadpilot(path, init = {}) {
  const res = await fetch(CADPILOT_URL + path, { ...init, headers: { "X-API-Key": API_KEY, ...(init.headers || {}) } });
  return res;
}

/** CadPilot links are built from its public URL; fetch the same path at the address we can reach. */
const viaCadpilot = (link) => {
  const u = new URL(link);
  return CADPILOT_URL + u.pathname + u.search;
};

// ---- routes ----------------------------------------------------------------------------------------

const routes = {
  "GET /api/config": async () => json(200, { cadpilot_url: CADPILOT_URL, callback_base: CALLBACK_BASE, has_key: !!API_KEY }),

  "GET /api/records": async () => json(200, Object.values(records).sort((a, b) => b.created_at.localeCompare(a.created_at))),

  // The CRM user clicked "Send to CadPilot": forward the two workbooks.
  "POST /api/records": async (req) => {
    const body = JSON.parse(await readBody(req));
    const form = new FormData();
    form.set("external_id", body.external_id);
    form.set("name", body.name || "");
    form.set("request", body.request || "");
    form.set("return_url", `${PUBLIC_BASE}/#${encodeURIComponent(body.external_id)}`);
    if (body.use_webhook) form.set("callback_url", `${CALLBACK_BASE}/webhook`);
    for (const role of ["mass_balance", "design_data"]) {
      const f = body[role];
      if (f) form.set(role, new Blob([Buffer.from(f.base64, "base64")]), f.name);
    }
    const res = await cadpilot("/api/integrations/crm/projects", { method: "POST", body: form });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) return json(res.status, { error: out.detail || `CadPilot answered ${res.status}` });
    const prev = records[body.external_id] || {};
    records[body.external_id] = {
      ...prev,
      external_id: body.external_id,
      name: body.name || prev.name || body.external_id,
      use_webhook: !!body.use_webhook,
      created_at: prev.created_at || new Date().toISOString(),
      files: { mass_balance: body.mass_balance?.name || prev.files?.mass_balance, design_data: body.design_data?.name || prev.files?.design_data },
      project_id: out.project_id,
      launch_url: out.launch_url,
      status: out,
    };
    await save();
    log(`sent ${body.external_id} → project ${out.project_id} (${out.created ? "new" : "updated"})`);
    return json(200, records[body.external_id]);
  },

  // Pull the latest status from CadPilot (works even when the webhook cannot reach us).
  "GET /api/records/:id/status": async (_req, id) => {
    const rec = records[id];
    if (!rec) return json(404, { error: "unknown record" });
    const res = await cadpilot(`/api/integrations/crm/projects/${encodeURIComponent(id)}`);
    if (!res.ok) return json(res.status, { error: `CadPilot answered ${res.status}` });
    rec.status = await res.json();
    await save();
    return json(200, rec);
  },

  // Download: the copy the webhook delivered, otherwise pull it from CadPilot now.
  "GET /api/records/:id/dxf": async (_req, id) => {
    const rec = records[id];
    if (!rec) return json(404, { error: "unknown record" });
    if (rec.received?.file && existsSync(join(DATA, "inbox", rec.received.file))) {
      return file(await readFile(join(DATA, "inbox", rec.received.file)), "application/dxf", rec.received.filename);
    }
    const res = await cadpilot(`/api/integrations/crm/projects/${encodeURIComponent(id)}/dxf`);
    if (!res.ok) return json(res.status, { error: res.status === 409 ? "Not approved yet" : `CadPilot answered ${res.status}` });
    const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") || "")?.[1] || `${id}.dxf`;
    return file(Buffer.from(await res.arrayBuffer()), "application/dxf", name);
  },

  // Preview of the approved drawing, through CadPilot's signed link.
  "GET /api/records/:id/preview.svg": async (_req, id) => {
    const link = records[id]?.status?.approved_revision?.svg_url;
    if (!link) return json(404, { error: "Not approved yet" });
    const res = await fetch(viaCadpilot(link));
    if (!res.ok) return json(res.status, { error: `CadPilot answered ${res.status}` });
    return { status: 200, headers: { "Content-Type": "image/svg+xml", "Cache-Control": "no-store" }, body: Buffer.from(await res.arrayBuffer()) };
  },

  "POST /api/records/:id/redeliver": async (_req, id) => {
    const res = await cadpilot(`/api/integrations/crm/projects/${encodeURIComponent(id)}/redeliver`, { method: "POST" });
    const out = await res.json().catch(() => ({}));
    return json(res.status, res.ok ? out : { error: out.detail || `CadPilot answered ${res.status}` });
  },

  // CadPilot → CRM: the approved drawing.
  "POST /webhook": async (req) => {
    const raw = await readBody(req);
    const ts = req.headers["x-cadpilot-timestamp"] || "";
    const got = String(req.headers["x-cadpilot-signature"] || "");
    const expected = "sha256=" + createHmac("sha256", SECRET).update(`${ts}.`).update(raw).digest("hex");
    const fresh = Math.abs(Date.now() / 1000 - Number(ts)) <= 300;
    if (!fresh || got.length !== expected.length || !timingSafeEqual(Buffer.from(got), Buffer.from(expected))) {
      log("webhook REJECTED: bad signature or stale timestamp");
      return json(401, { error: "bad signature" });
    }
    const msg = JSON.parse(raw.toString());
    const stored = `${msg.external_id.replace(/[^\w.-]/g, "_")}_rev${msg.revision}.dxf`;
    await writeFile(join(DATA, "inbox", stored), Buffer.from(msg.dxf.content_base64, "base64"));
    const rec = (records[msg.external_id] ||= { external_id: msg.external_id, name: msg.project_name, created_at: new Date().toISOString() });
    delete msg.dxf.content_base64;
    rec.received = { file: stored, filename: msg.dxf.filename, size: msg.dxf.size, revision: msg.revision, approved_by: msg.approved_by, at: new Date().toISOString() };
    rec.status = { ...(rec.status || {}), state: "approved", approved_revision: { revision: msg.revision, approved_by: msg.approved_by, approved_at: msg.approved_at, dxf_url: msg.dxf.url, svg_url: msg.svg_url } };
    await save();
    log(`webhook: received ${msg.dxf.filename} (${msg.dxf.size} bytes) for ${msg.external_id}`);
    return json(200, { ok: true });
  },
};

// ---- plumbing --------------------------------------------------------------------------------------

const json = (status, data) => ({ status, headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
const file = (buf, type, name) => ({ status: 200, headers: { "Content-Type": type, "Content-Disposition": `attachment; filename="${name}"` }, body: buf });
const readBody = (req) => new Promise((ok, fail) => { const c = []; req.on("data", (d) => c.push(d)); req.on("end", () => ok(Buffer.concat(c))); req.on("error", fail); });
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css" };

function match(method, path) {
  for (const [key, fn] of Object.entries(routes)) {
    const [m, pattern] = key.split(" ");
    if (m !== method) continue;
    const re = new RegExp("^" + pattern.replace(/:id/, "([^/]+)") + "$");
    const hit = re.exec(path);
    if (hit) return (req) => fn(req, hit[1] && decodeURIComponent(hit[1]));
  }
  return null;
}

createServer(async (req, res) => {
  const path = new URL(req.url, "http://x").pathname;
  try {
    const handler = match(req.method, path);
    let out;
    if (handler) out = await handler(req);
    else if (req.method === "GET") {
      const f = join(HERE, "public", path === "/" ? "index.html" : path.replace(/\.\./g, ""));
      out = existsSync(f) ? { status: 200, headers: { "Content-Type": TYPES[extname(f)] || "application/octet-stream" }, body: await readFile(f) } : json(404, { error: "not found" });
    } else out = json(404, { error: "not found" });
    res.writeHead(out.status, out.headers);
    res.end(out.body);
  } catch (e) {
    log("error:", e.message);
    res.writeHead(502, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: `Could not reach CadPilot at ${CADPILOT_URL}: ${e.cause?.code || e.message}` }));
  }
}).listen(PORT, () => {
  log(`Test CRM on http://localhost:${PORT}`);
  log(`CadPilot API: ${CADPILOT_URL}   webhook: ${CALLBACK_BASE}/webhook   key: ${API_KEY ? "set" : "MISSING (set CADPILOT_CRM_API_KEY)"}`);
});
