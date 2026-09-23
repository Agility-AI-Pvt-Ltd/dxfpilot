// Test CRM page. Talks only to its own server (server.mjs), never to CadPilot directly.

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const crmPath = (path) => new URL(String(path).replace(/^\//, ""), document.baseURI).toString();

async function call(path, init) {
  const res = await fetch(crmPath(path), init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const toBase64 = (file) =>
  new Promise((ok, fail) => {
    const r = new FileReader();
    r.onload = () => ok({ name: file.name, base64: String(r.result).split(",")[1] });
    r.onerror = fail;
    r.readAsDataURL(file);
  });

// ---- 1. send a record's workbooks to CadPilot ----------------------------------------------------

$("#send").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const msg = $("#send-msg");
  const btn = f.querySelector("button[type=submit]");
  const body = {
    external_id: f.external_id.value.trim(),
    name: f.name.value.trim(),
    request: f.request.value.trim(),
    use_webhook: f.use_webhook.checked,
  };
  for (const role of ["mass_balance", "design_data"]) if (f[role].files[0]) body[role] = await toBase64(f[role].files[0]);
  btn.disabled = true;
  msg.className = "small muted";
  msg.textContent = "Uploading and reading the workbooks…";
  try {
    const rec = await call("/api/records", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    msg.className = "small ok";
    msg.innerHTML = `Sent. <a href="${esc(rec.launch_url)}" target="_blank" rel="noopener">Open ${esc(rec.external_id)} in CadPilot →</a>`;
    await load();
  } catch (err) {
    msg.className = "small err";
    msg.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

// ---- 2. records: status, open in CadPilot, preview and download --------------------------------

const STATE = { draft_pending: "Draft pending", in_review: "In review", approved: "Approved" };

function render(records) {
  const box = $("#records");
  if (!records.length) return (box.innerHTML = '<p class="muted">No records yet.</p>');
  box.innerHTML = records
    .map((r) => {
      const s = r.status || {};
      const ap = s.approved_revision;
      const last = (s.deliveries || []).at(-1);
      const id = encodeURIComponent(r.external_id);
      const delivery = r.received
        ? `<span class="ok">Webhook received rev ${esc(r.received.revision)} · ${new Date(r.received.at).toLocaleString()}</span>`
        : last
          ? last.ok ? `<span class="ok">CadPilot delivered rev ${esc(last.revision)}</span>` : `<span class="err">Delivery failed: ${esc(last.detail || last.http_status)}</span>`
          : r.use_webhook ? '<span class="muted">Waiting for approval → webhook</span>' : '<span class="muted">Pull mode (no webhook)</span>';
      return `
      <article class="record" id="${esc(r.external_id)}">
        <div>
          <h3>${esc(r.name)} <span class="badge ${esc(s.state)}">${esc(STATE[s.state] || s.state || "unknown")}</span></h3>
          <dl>
            <dt>Record</dt><dd>${esc(r.external_id)}</dd>
            <dt>Project</dt><dd>${esc(r.project_id)} · ${esc(s.drawing_number || "")}</dd>
            <dt>Workbooks</dt><dd>${esc(r.files?.mass_balance)} · ${esc(r.files?.design_data)}</dd>
            <dt>Current</dt><dd>${s.current_revision ? `rev ${esc(s.current_revision)} (${esc(s.current_status)})` : "no draft yet — opens and drafts on first visit"}</dd>
            <dt>Approved</dt><dd>${ap ? `rev ${esc(ap.revision)} by ${esc(ap.approved_by)} · ${new Date(ap.approved_at).toLocaleString()}` : "—"}</dd>
            <dt>Delivery</dt><dd>${delivery}</dd>
          </dl>
          <div class="actions">
            ${r.launch_url ? `<a class="btn primary" href="${esc(r.launch_url)}" target="_blank" rel="noopener">Open in CadPilot</a>` : ""}
            <button data-act="status" data-id="${id}">Check status</button>
            ${ap ? `<a class="btn" href="${esc(crmPath(`/api/records/${id}/dxf`))}">Download DXF</a>` : ""}
            ${ap && r.use_webhook ? `<button data-act="redeliver" data-id="${id}">Ask CadPilot to resend</button>` : ""}
          </div>
        </div>
        <div class="preview">
          ${ap ? `<a href="${esc(crmPath(`/api/records/${id}/preview.svg`))}" target="_blank"><img alt="Approved P&amp;ID preview" src="${esc(crmPath(`/api/records/${id}/preview.svg`))}?rev=${esc(ap.revision)}" /></a>` : '<span class="muted small">Preview appears after approval</span>'}
        </div>
      </article>`;
    })
    .join("");
}

$("#records").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  b.disabled = true;
  try {
    if (b.dataset.act === "status") await call(`/api/records/${b.dataset.id}/status`);
    else {
      await call(`/api/records/${b.dataset.id}/redeliver`, { method: "POST" });
      await new Promise((r) => setTimeout(r, 2500));
    }
    await load();
  } catch (err) {
    alert(err.message);
    b.disabled = false;
  }
});

async function load() {
  render(await call("/api/records"));
}

// Refresh every record's status from CadPilot (pull), e.g. after approving in the other tab.
async function refreshAll() {
  const records = await call("/api/records");
  await Promise.all(records.filter((r) => r.project_id).map((r) => call(`/api/records/${encodeURIComponent(r.external_id)}/status`).catch(() => null)));
  await load();
}
$("#refresh").addEventListener("click", refreshAll);
// Coming back from the CadPilot tab: pick up the approval without a click
document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && refreshAll());

call("/api/config").then((c) => {
  $("#config").innerHTML = `CadPilot API: <b>${esc(c.cadpilot_url)}</b> · key ${c.has_key ? "set" : '<span class="err">missing</span>'}`;
  document.querySelector("[name=use_webhook]").checked = /localhost|127\.0\.0\.1/.test(c.cadpilot_url);
});
refreshAll().catch(() => load());
