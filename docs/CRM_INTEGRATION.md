# CRM integration

The CRM hands a record's two workbooks to CadPilot. Its users open the drawing in CadPilot from a link, CadPilot drafts the P&ID, and a reviewer approves it. The approved DXF then goes back to the CRM, where users can see and download it.

```
CRM server ──POST /api/integrations/crm/projects  (X-API-Key, mass_balance.xlsx + design_data.xlsx)──▶ CadPilot
           ◀── { project_id, launch_url, status_url } ──   (store launch_url on the CRM record)

CRM user   ──clicks "Open P&ID" (launch_url)──▶ CadPilot workspace
                 · New P&ID draft page, prefilled: project name + both workbooks → "Generate draft"
                 · banner: "From CRM record LEAD-42 · on approval the DXF is sent to the CRM"
                 · reviewer corrects in chat, then clicks Approve

CadPilot   ──POST callback_url  (signed JSON: DXF inline + signed download/preview links)──▶ CRM server
CRM page   shows the preview (svg_url) and a Download DXF button (its stored copy, or dxf_url)
```

## Settings (CadPilot server, `/opt/dextool/.env`)

| Variable | Purpose |
|---|---|
| `CADPILOT_PUBLIC_URL` | The address users open CadPilot at, e.g. `http://13.233.x.x` or `https://pid.example.com`. Launch and download links are built from it. |
| `CADPILOT_CRM_API_KEY` | Shared key the CRM **server** sends as `X-API-Key` (or `Authorization: Bearer …`). While it is empty the integration is off and returns 503. Generate it with `openssl rand -hex 32`. |
| `CADPILOT_CRM_WEBHOOK_SECRET` | Signs the approval webhook and the download links. Defaults to the API key. Share it with the CRM team. |
| `CADPILOT_CORS_ORIGINS` | Optional: the CRM's web origin(s), only if CRM pages call CadPilot from the browser. |

Redeploy after changing them. The deploy job restarts the containers, and PostgreSQL migration 4 (`projects.integration`) runs on its own.

> Keep the API key on the CRM **server**. Never put it in CRM browser code.

## 1. Hand over a record: `POST /api/integrations/crm/projects`

The request is `multipart/form-data` with these fields:

| Field | Required | |
|---|---|---|
| `external_id` | yes | The CRM record id (for example the lead or opportunity id). Calls are **idempotent** on it: sending the same record again updates the same project, and new workbooks replace the old ones. |
| `mass_balance` | yes (new record) | `.xlsx` |
| `design_data` | yes (new record) | `.xlsx`: the equipment list and design criteria |
| `name` | no | Project name shown in CadPilot |
| `callback_url` | no | Where CadPilot POSTs the approved drawing. Without it, the CRM polls the status endpoint instead. |
| `return_url` | no | The "← Back to CRM" link in the CadPilot workspace, typically the record's page |
| `request` | no | Instruction for the first draft, for example "Draw reception, pasteurization and CIP". By default CadPilot detects the plant sections from the workbooks. |

```bash
curl -X POST "$CADPILOT/api/integrations/crm/projects" \
  -H "X-API-Key: $CADPILOT_CRM_API_KEY" \
  -F external_id=LEAD-42 \
  -F name="Sunrise Dairy 5 LLPD" \
  -F callback_url=https://crm.example.com/webhooks/cadpilot \
  -F return_url=https://crm.example.com/leads/42 \
  -F mass_balance=@Mass_Balance.xlsx \
  -F design_data=@Design_Data.xlsx
```

```json
{
  "project_id": "8f453f24f363",
  "external_id": "LEAD-42",
  "created": true,
  "state": "draft_pending",
  "launch_url": "https://pid.example.com/?p=8f453f24f363&from=crm",
  "status_url": "https://pid.example.com/api/integrations/crm/projects/LEAD-42",
  "approved_revision": null,
  "deliveries": []
}
```

Store `launch_url` on the CRM record, and make the record's **Open P&ID** button a plain link to it (`target="_blank"` if you like).

While the record has no draft, the link opens CadPilot's **New P&ID draft** page with the project name and both workbooks already filled in ("received from the CRM"). The reviewer can rename the project, replace a workbook, change the instruction, plant sections or engine, and then clicks **Generate draft**. Once a draft exists, the same link opens the workspace directly.

Errors:
- `401`: wrong key.
- `503`: the integration is off.
- `400`: a missing workbook, a file that is not `.xlsx`, a workbook that cannot be read, or a bad URL.

The body limit at the gateway is 25 MB.

## 2. The approval webhook (CadPilot → `callback_url`)

This is sent when a reviewer approves a revision. It is also sent again for each later approved revision.

```
POST <callback_url>
Content-Type: application/json
X-CadPilot-Event: drawing.approved
X-CadPilot-Timestamp: 1790000000
X-CadPilot-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>
```

```json
{
  "event": "drawing.approved",
  "external_id": "LEAD-42",
  "project_id": "8f453f24f363",
  "project_name": "Sunrise Dairy 5 LLPD",
  "drawing_number": "CP-PID-8F45-001",
  "revision": "A",
  "approved_by": "reviewer",
  "approved_at": "2026-09-22T05:33:14+00:00",
  "counts": {"equipment": 75, "lines": 137, "instruments": 179, "control_loops": 41},
  "warnings": 1,
  "dxf": {
    "filename": "CP-PID-8F45-001_revA.dxf",
    "content_type": "application/dxf",
    "size": 544902,
    "content_base64": "…",
    "url": "https://pid.example.com/api/integrations/crm/files/8f453f24f363/A.dxf?token=…"
  },
  "svg_url": "https://pid.example.com/api/integrations/crm/files/8f453f24f363/A.svg?token=…",
  "launch_url": "https://pid.example.com/?p=8f453f24f363&from=crm"
}
```

What the CRM should do:
1. Verify the signature over the **raw** body. Reject timestamps more than 5 minutes old.
2. Decode `dxf.content_base64` and store the file against `external_id`.
3. Answer `2xx` quickly.

CadPilot tries 3 times (after 2 s and 4 s) and records each outcome. The workspace banner shows "delivered" or "failed · Send again".

Verification in Node (Express, raw body):

```js
const crypto = require("crypto");
app.post("/webhooks/cadpilot", express.raw({ type: "application/json", limit: "30mb" }), (req, res) => {
  const ts = req.get("X-CadPilot-Timestamp");
  const expected = "sha256=" + crypto.createHmac("sha256", process.env.CADPILOT_WEBHOOK_SECRET)
    .update(ts + "." + req.body).digest("hex");
  const got = req.get("X-CadPilot-Signature") || "";
  if (Math.abs(Date.now() / 1000 - Number(ts)) > 300 ||
      got.length !== expected.length || !crypto.timingSafeEqual(Buffer.from(got), Buffer.from(expected)))
    return res.sendStatus(401);
  const msg = JSON.parse(req.body);
  saveDrawing(msg.external_id, msg.dxf.filename, Buffer.from(msg.dxf.content_base64, "base64"), msg);
  res.sendStatus(200);
});
```

`backend/examples/mock_crm.py` has the same check in Python. It is a working stand-in for the CRM.

## 3. Show and download it on the CRM page

- **Preview:** `<img src="{svg_url}">` shows the approved drawing (SVG).
- **Download:** link to your stored copy of the DXF, or directly to `dxf.url`.

These links carry a token that is signed per project, revision and file type. Only **approved** revisions are served, and no API key is needed in the browser. Anyone who has a link can open that one drawing, so treat the links like the drawing itself.

## 4. Pull instead of (or as well as) the webhook

The CRM server can poll or fetch these with its API key:

| | |
|---|---|
| `GET /api/integrations/crm/projects/{external_id}` | Status: `state` is `draft_pending`, then `in_review`, then `approved`. It also returns `current_revision`, `approved_revision` (with `dxf_url` and `svg_url`) and `deliveries`. |
| `GET /api/integrations/crm/projects/{external_id}/dxf` | The latest approved DXF. Returns `409` until something is approved. |
| `POST /api/integrations/crm/projects/{external_id}/redeliver` | Send the latest approved drawing to `callback_url` again. |

## Try it locally

```bash
# terminal 1 — CadPilot API with the integration on
cd backend && CADPILOT_CRM_API_KEY=dev-key CADPILOT_PUBLIC_URL=http://localhost:3000 uv run uvicorn cadpilot.api.main:app --port 8000
# terminal 2 — the web app
cd frontend && npm run dev
# terminal 3 — the mock CRM (receives approved drawings, lists them on http://localhost:8765)
cd backend && CADPILOT_CRM_API_KEY=dev-key uv run python examples/mock_crm.py serve
# terminal 4 — hand over a record, then open the printed launch link, review and approve
cd backend && CADPILOT_CRM_API_KEY=dev-key uv run python examples/mock_crm.py send LEAD-1 ../data/Mass_Balance_Dummy_Data.xlsx ../data/Dairy_Plant_Estimation_Dummy_Data.xlsx
```

## Notes

- CadPilot itself has no user login yet. Anyone who can reach the site can open a project by its link. Restrict port 80/443 to your network (security group), or put CadPilot behind the CRM's SSO proxy, before exposing real client data.
- If the CRM sends new workbooks after a draft exists, they replace the stored files. The reviewer then clicks **Design data → Regenerate** to redraw.
