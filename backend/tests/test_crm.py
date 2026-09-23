"""CRM integration: workbooks in from the CRM, approved DXF back out."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

os.environ["OPENAI_API_KEY"] = ""
os.environ["LANGSMITH_TRACING"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cadpilot.api import main as api  # noqa: E402
from cadpilot.integrations import crm  # noqa: E402
from cadpilot.service import DEMO_FILES, CadPilot  # noqa: E402
from cadpilot.store import FileStore  # noqa: E402

KEY = "test-key"
H = {"X-API-Key": KEY}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CADPILOT_CRM_API_KEY", KEY)
    monkeypatch.setenv("CADPILOT_CRM_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("CADPILOT_PUBLIC_URL", "https://pid.example.com")
    monkeypatch.setattr(api, "_pilot", CadPilot(FileStore(tmp_path)))
    return TestClient(api.app)


def files():
    return {role: (path.name, path.read_bytes()) for role, path in DEMO_FILES.items()}


def create(client, external_id="LEAD-42", **form):
    data = {"external_id": external_id, "name": "Amul dairy 5 LLPD", "callback_url": "https://crm.example.com/hook",
            "return_url": "https://crm.example.com/leads/42", **form}
    return client.post("/api/integrations/crm/projects", data=data, files=files(), headers=H)


def test_the_crm_needs_the_api_key(client, monkeypatch):
    assert client.post("/api/integrations/crm/projects", data={"external_id": "x"}).status_code == 401
    assert client.get("/api/integrations/crm/projects/x", headers={"X-API-Key": "wrong"}).status_code == 401
    monkeypatch.setenv("CADPILOT_CRM_API_KEY", "")
    assert client.get("/api/integrations/crm/projects/x", headers=H).status_code == 503


def test_intake_creates_one_project_per_record_and_links_to_it(client):
    r = create(client)
    assert r.status_code == 200, r.text
    out = r.json()
    pid = out["project_id"]
    assert out["created"] and out["state"] == "draft_pending"
    assert out["launch_url"] == f"https://pid.example.com/?p={pid}&from=crm"
    assert out["status_url"] == "https://pid.example.com/api/integrations/crm/projects/LEAD-42"
    view = client.get(f"/api/projects/{pid}").json()
    assert view["source_summary"]["equipment_rows"] > 0 and set(view["sources"]) == {"mass_balance", "design_data"}
    assert view["integration"]["external_id"] == "LEAD-42" and view["integration"]["has_callback"]
    assert "callback_url" not in view["integration"]  # the workspace never sees where the CRM listens
    # the same record again updates, never duplicates
    again = create(client, name="Renamed").json()
    assert again["project_id"] == pid and not again["created"] and again["name"] == "Renamed"
    # a new record must bring both workbooks
    r = client.post("/api/integrations/crm/projects", data={"external_id": "LEAD-43"}, headers=H)
    assert r.status_code == 400


def test_approval_sends_the_signed_dxf_to_the_crm_and_the_crm_can_pull_it(client, monkeypatch):
    pid = create(client).json()["project_id"]
    rev = api.pilot().run_generation(pid, "Generate the P&ID")
    assert client.get("/api/integrations/crm/projects/LEAD-42", headers=H).json()["state"] == "in_review"
    assert client.get("/api/integrations/crm/projects/LEAD-42/dxf", headers=H).status_code == 409

    sent = []
    monkeypatch.setattr(crm, "_http_post", lambda url, body, headers: sent.append((url, body, headers)) or (200, "ok"))
    monkeypatch.setattr(api.threading, "Thread", _Inline)
    r = client.post(f"/api/projects/{pid}/revisions/{rev.revision}/approve")
    assert r.json()["delivering"]

    url, body, headers = sent[0]
    assert url == "https://crm.example.com/hook"
    expected = "sha256=" + hmac.new(b"whsec", headers["X-CadPilot-Timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
    assert headers["X-CadPilot-Signature"] == expected
    msg = json.loads(body)
    assert msg["event"] == "drawing.approved" and msg["external_id"] == "LEAD-42" and msg["revision"] == rev.revision
    dxf = base64.b64decode(msg["dxf"]["content_base64"])
    assert is_dxf(dxf) and msg["dxf"]["size"] == len(dxf)

    status = client.get("/api/integrations/crm/projects/LEAD-42", headers=H).json()
    assert status["state"] == "approved" and status["deliveries"][-1]["ok"]
    assert client.get(f"/api/projects/{pid}").json()["integration"]["deliveries"][-1]["ok"]
    # pull: with the API key, or through the signed links the CRM can put on its own page
    assert is_dxf(client.get("/api/integrations/crm/projects/LEAD-42/dxf", headers=H).content)
    links = status["approved_revision"]
    assert is_dxf(client.get(links["dxf_url"].replace("https://pid.example.com", "")).content)
    assert client.get(links["svg_url"].replace("https://pid.example.com", "")).headers["content-type"].startswith("image/svg")
    forged = links["dxf_url"].replace("https://pid.example.com", "").split("token=")[0] + "token=nope"
    assert client.get(forged).status_code == 404


def test_a_failed_delivery_is_recorded_and_can_be_retried(client, monkeypatch):
    pid = create(client).json()["project_id"]
    rev = api.pilot().run_generation(pid, "Generate the P&ID")
    monkeypatch.setattr(api.threading, "Thread", _Inline)
    calls = []

    def down(url, body, headers):
        calls.append(1)
        return 502, "bad gateway"

    monkeypatch.setattr(crm, "_http_post", down)
    monkeypatch.setattr(crm.time, "sleep", lambda _s: None)
    client.post(f"/api/projects/{pid}/revisions/{rev.revision}/approve")
    last = client.get(f"/api/projects/{pid}").json()["integration"]["deliveries"][-1]
    assert len(calls) == crm.MAX_ATTEMPTS and not last["ok"] and last["http_status"] == 502

    monkeypatch.setattr(crm, "_http_post", lambda *_: (204, ""))
    assert client.post(f"/api/projects/{pid}/integration/redeliver").json()["delivering"]
    assert client.get(f"/api/projects/{pid}").json()["integration"]["deliveries"][-1]["ok"]


def test_projects_created_in_cadpilot_are_not_sent_anywhere(client):
    pilot = api.pilot()
    pid = pilot.create_project("local", demo_data=True).info.id
    rev = pilot.run_generation(pid, "Generate the P&ID")
    r = client.post(f"/api/projects/{pid}/revisions/{rev.revision}/approve")
    assert r.json() == {"revision": rev.revision, "status": "approved", "delivering": False}
    assert client.get(f"/api/projects/{pid}").json()["integration"] is None


def is_dxf(data: bytes) -> bool:
    return data.startswith(b"  0\nSECTION") and data.rstrip().endswith(b"EOF") and len(data) > 10_000


class _Inline:
    """threading.Thread stand-in that runs the target at start(), so tests see the result."""

    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


def test_the_reviewer_can_rename_a_handed_over_project(client):
    pid = create(client).json()["project_id"]
    assert client.patch(f"/api/projects/{pid}", json={"name": "  Sunrise — phase 1 "}).json()["name"] == "Sunrise — phase 1"
    assert client.patch(f"/api/projects/{pid}", json={"name": " "}).status_code == 400
    assert client.patch("/api/projects/nope000000", json={"name": "x"}).status_code == 404
    assert client.get("/api/integrations/crm/projects/LEAD-42", headers=H).json()["name"] == "Sunrise — phase 1"
