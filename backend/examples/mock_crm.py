"""A stand-in for the CRM, to try the integration end to end (standard library only).

    # 1. terminal A: the mock CRM (receives approved drawings, shows them on http://localhost:8765)
    python examples/mock_crm.py serve

    # 2. terminal B: hand a record's two workbooks to CadPilot, then open the printed launch link
    python examples/mock_crm.py send LEAD-1001 ../data/<mass balance>.xlsx ../data/<design data>.xlsx

Settings: CADPILOT_API (default http://localhost:8000), CADPILOT_CRM_API_KEY,
CADPILOT_CRM_WEBHOOK_SECRET (same values as the CadPilot server), MOCK_CRM_PORT (8765).

The webhook handler shows what a real CRM must do: verify the signature, check the timestamp,
store the DXF against the record (external_id), and answer 2xx quickly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import sys
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API = os.environ.get("CADPILOT_API", "http://localhost:8000").rstrip("/")
KEY = os.environ.get("CADPILOT_CRM_API_KEY", "")
SECRET = (os.environ.get("CADPILOT_CRM_WEBHOOK_SECRET") or KEY).encode()
PORT = int(os.environ.get("MOCK_CRM_PORT", "8765"))
INBOX = Path(__file__).resolve().parent / "mock_crm_inbox"


def verify(body: bytes, timestamp: str, signature: str) -> bool:
    if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > 300:  # replay window: 5 minutes
        return False
    expected = "sha256=" + hmac.new(SECRET, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/hook":
            return self._send(404, b"not found")
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not verify(body, self.headers.get("X-CadPilot-Timestamp", ""), self.headers.get("X-CadPilot-Signature", "")):
            return self._send(401, b"bad signature")
        msg = json.loads(body)
        folder = INBOX / msg["external_id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / msg["dxf"]["filename"]).write_bytes(base64.b64decode(msg["dxf"]["content_base64"]))
        msg["dxf"].pop("content_base64")
        (folder / "latest.json").write_text(json.dumps(msg, indent=1))
        print(f"received {msg['dxf']['filename']} for {msg['external_id']} (rev {msg['revision']}, approved by {msg['approved_by']})")
        self._send(200, b"ok")

    def do_GET(self) -> None:
        if self.path.startswith("/files/"):
            f = INBOX / self.path.removeprefix("/files/")
            if f.resolve().is_relative_to(INBOX.resolve()) and f.is_file():
                return self._send(200, f.read_bytes(), "application/dxf", {"Content-Disposition": f'attachment; filename="{f.name}"'})
            return self._send(404, b"not found")
        rows = []
        for meta in sorted(INBOX.glob("*/latest.json")):
            m = json.loads(meta.read_text())
            e = html.escape
            rows.append(
                f"<tr><td><b>{e(m['external_id'])}</b><br>{e(m['project_name'])}</td><td>{e(m['drawing_number'])} rev {e(m['revision'])}"
                f"<br>approved by {e(str(m['approved_by']))}</td>"
                f"<td><a href='/files/{e(m['external_id'])}/{e(m['dxf']['filename'])}'>Download DXF</a> · "
                f"<a href='{e(m['launch_url'])}'>Open in CadPilot</a></td>"
                f"<td><a href='{e(m['svg_url'])}' target='_blank'><img src='{e(m['svg_url'])}' width='320'></a></td></tr>"
            )
        page = (
            "<!doctype html><meta charset=utf-8><title>Mock CRM</title><body style='font:14px system-ui;margin:24px'>"
            "<h2>Mock CRM — approved P&amp;IDs</h2><table border=1 cellpadding=8 style='border-collapse:collapse'>"
            "<tr><th>Record</th><th>Drawing</th><th>Files</th><th>Preview</th></tr>"
            + ("".join(rows) or "<tr><td colspan=4>Nothing approved yet</td></tr>") + "</table>"
        )
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def _send(self, code: int, body: bytes, ctype: str = "text/plain", headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def send(external_id: str, mass_balance: str, design_data: str) -> None:
    """What the CRM server does when a user clicks "Draft P&ID" on a record."""
    boundary = uuid.uuid4().hex
    parts = []
    fields = {"external_id": external_id, "name": f"CRM record {external_id}",
              "callback_url": f"http://localhost:{PORT}/hook", "return_url": f"http://localhost:{PORT}/"}
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for role, path in (("mass_balance", mass_balance), ("design_data", design_data)):
        p = Path(path)
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{role}"; filename="{p.name}"\r\n'
            "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n".encode()
            + p.read_bytes() + b"\r\n"
        )
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{API}/api/integrations/crm/projects", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "X-API-Key": KEY},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.loads(resp.read())
    print(json.dumps({k: out[k] for k in ("project_id", "created", "state", "launch_url", "status_url")}, indent=1))
    print(f"\nOpen this link (what the CRM button does):\n  {out['launch_url']}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        INBOX.mkdir(exist_ok=True)
        print(f"mock CRM on http://localhost:{PORT}  (webhook: POST /hook)")
        ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    elif len(sys.argv) == 5 and sys.argv[1] == "send":
        send(*sys.argv[2:])
    else:
        sys.exit(__doc__)
