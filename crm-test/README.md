# Test CRM for the CadPilot integration

A small stand-in for the CRM website, used to try the whole flow by hand:

1. **Send.** Fill in a record, attach the two Excel files and click **Send to CadPilot**.
2. **Open.** Click **Open in CadPilot**. The draft is made automatically; review it and click **Approve**.
3. **Receive.** Come back to this tab. The approved drawing's preview and a **Download DXF** button appear.

It runs on Node 18 or newer with no `npm install`:

- `public/` holds the CRM page (HTML/JS).
- `server.mjs` plays the CRM's backend. It keeps the API key, calls CadPilot and receives the approval webhook.

## Run against CadPilot on this machine

```bash
# terminal 1 — CadPilot API (integration on)
cd backend
CADPILOT_CRM_API_KEY=dev-key CADPILOT_CRM_WEBHOOK_SECRET=dev-secret CADPILOT_PUBLIC_URL=http://localhost:3000 \
  uv run uvicorn cadpilot.api.main:app --port 8000

# terminal 2 — CadPilot web app
cd frontend && npm run dev

# terminal 3 — this test CRM → http://localhost:4000
cd crm-test
CADPILOT_URL=http://localhost:8000 CADPILOT_CRM_API_KEY=dev-key CADPILOT_CRM_WEBHOOK_SECRET=dev-secret node server.mjs
```

Tick **Receive the approved DXF by webhook** (on by default here). CadPilot then pushes the DXF to `http://localhost:4000/webhook` when you approve.

## Run against CadPilot on EC2

Use the key and secret from `/opt/dextool/.env` on the server:

```bash
CADPILOT_URL=http://<ec2-address> CADPILOT_CRM_API_KEY=<key> CADPILOT_CRM_WEBHOOK_SECRET=<secret> node server.mjs
```

EC2 cannot reach `localhost` on your laptop, so leave the webhook box **unticked**. The page then pulls the status and the DXF from CadPilot; press **Check status** or just switch back to the tab.

To test the webhook against EC2 as well, expose this server publicly, for example with `ngrok http 4000`. Then start it with `CALLBACK_BASE=https://<id>.ngrok.app` and tick the box.

## Files

| | |
|---|---|
| `public/index.html`, `app.js`, `style.css` | The CRM page. It only talks to `server.mjs`. |
| `server.mjs` | Forwards the workbooks, checks status, downloads the DXF, and verifies and stores webhook deliveries. |
| `data/` | Created at run time: `records.json` and the received DXF files in `inbox/`. Not committed. |
