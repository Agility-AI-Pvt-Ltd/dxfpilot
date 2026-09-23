"""CRM integration: the CRM hands over a record's two workbooks, its users review the draft in
CadPilot, and the approved DXF goes back to the CRM.

    CRM server ──POST /api/integrations/crm/projects (API key, 2 × .xlsx)──▶ CadPilot
               ◀── {project_id, launch_url, status_url, …} ──
    CRM user   ──clicks launch_url──▶ CadPilot workspace (auto-drafts, reviewer approves)
    CadPilot   ──POST callback_url (signed JSON, DXF inline + download links)──▶ CRM server
    CRM        ──GET status_url / dxf_url (pull, if the webhook was missed)──▶ CadPilot

Settings (environment):
  CADPILOT_PUBLIC_URL           address users reach CadPilot at, e.g. https://pid.example.com
  CADPILOT_CRM_API_KEY          shared key the CRM sends as `X-API-Key` (integration off when empty)
  CADPILOT_CRM_WEBHOOK_SECRET   signs callbacks and download links (defaults to the API key)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse

from ..store import Delivery, Integration, now_iso

log = logging.getLogger(__name__)

SOURCE = "crm"
MAX_ATTEMPTS = 3

Poster = Callable[[str, bytes, dict[str, str]], tuple[int, str]]


class IntegrationDisabled(RuntimeError):
    pass


def api_key() -> str:
    return os.environ.get("CADPILOT_CRM_API_KEY", "").strip()


def _secret() -> bytes:
    return (os.environ.get("CADPILOT_CRM_WEBHOOK_SECRET", "").strip() or api_key()).encode()


def public_url() -> str:
    return os.environ.get("CADPILOT_PUBLIC_URL", "http://localhost:3000").strip().rstrip("/")


def check_key(provided: str | None) -> None:
    """Raises IntegrationDisabled when no key is configured, PermissionError when the key is wrong."""
    key = api_key()
    if not key:
        raise IntegrationDisabled("CRM integration is not configured (set CADPILOT_CRM_API_KEY)")
    if not provided or not hmac.compare_digest(provided.encode(), key.encode()):
        raise PermissionError("Invalid API key")


def check_url(url: str | None, field: str) -> str | None:
    url = (url or "").strip() or None
    if url and (urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc):
        raise ValueError(f"{field} must be an http(s) URL")
    return url


# ---- links ------------------------------------------------------------------------------------

def file_token(pid: str, letter: str, kind: str) -> str:
    """Unguessable token for one approved file, so the CRM can link it without the API key."""
    return hmac.new(_secret(), f"{pid}:{letter}:{kind}".encode(), hashlib.sha256).hexdigest()[:40]


def check_file_token(pid: str, letter: str, kind: str, token: str) -> bool:
    return bool(_secret()) and hmac.compare_digest(file_token(pid, letter, kind), token or "")


def launch_url(pid: str) -> str:
    return f"{public_url()}/?p={pid}&from=crm"


def file_url(pid: str, letter: str, kind: str) -> str:
    return f"{public_url()}/api/integrations/crm/files/{pid}/{letter}.{kind}?token={file_token(pid, letter, kind)}"


# ---- intake -----------------------------------------------------------------------------------

def intake(pilot, external_id: str, name: str, files: dict[str, tuple[str, bytes]], callback_url: str | None,
           return_url: str | None, request: str) -> tuple[str, bool]:
    """Create (or update) the project for a CRM record. Returns (project id, created)."""
    external_id = external_id.strip()
    if not external_id or len(external_id) > 200:
        raise ValueError("external_id is required (at most 200 characters)")
    callback_url = check_url(callback_url, "callback_url")
    return_url = check_url(return_url, "return_url")
    pid = pilot.store.find_external(SOURCE, external_id)
    created = pid is None
    if created:
        pid = pilot.create_project(name.strip() or f"CRM {external_id}").info.id
    rec = pilot.store.get(pid)
    old = rec.integration
    rec.integration = Integration(
        source=SOURCE,
        external_id=external_id,
        callback_url=callback_url if callback_url is not None else old and old.callback_url,
        return_url=return_url if return_url is not None else old and old.return_url,
        request=request.strip() or (old.request if old else ""),
        created_at=old.created_at if old else now_iso(),
        deliveries=old.deliveries if old else [],
    )
    if name.strip():
        rec.info.name = name.strip()
    pilot.store.save(rec)
    if files:
        pilot.set_sources(pid, files)
    return pid, created


# ---- status -----------------------------------------------------------------------------------

def approved_revision(pilot, pid: str) -> str | None:
    """Latest approved revision (approved revisions are never superseded)."""
    approved = [r.revision for r in pilot.store.list_revisions(pid) if r.status == "approved"]
    return approved[-1] if approved else None


def status(pilot, pid: str) -> dict[str, Any]:
    rec = pilot.store.get(pid)
    integ = rec.integration
    approved = approved_revision(pilot, pid)
    current = pilot.store.revision(pid, rec.current) if rec.current else None
    out: dict[str, Any] = {
        "project_id": pid,
        "external_id": integ.external_id if integ else None,
        "name": rec.info.name,
        "drawing_number": rec.info.drawing_number,
        "launch_url": launch_url(pid),
        "sources": rec.sources,
        # draft_pending → in_review → approved (a newer draft after approval shows in current_revision)
        "state": "approved" if approved else ("in_review" if current else "draft_pending"),
        "current_revision": current.revision if current else None,
        "current_status": current.status if current else None,
        "approved_revision": None,
        "deliveries": [d.model_dump() for d in integ.deliveries] if integ else [],
    }
    if approved:
        rev = pilot.store.revision(pid, approved)
        out["approved_revision"] = {
            "revision": approved,
            "approved_by": rev.approved_by,
            "approved_at": rev.approved_at,
            "dxf_url": file_url(pid, approved, "dxf"),
            "svg_url": file_url(pid, approved, "svg"),
        }
    return out


# ---- delivery ---------------------------------------------------------------------------------

def payload(pilot, pid: str, letter: str) -> dict[str, Any]:
    rec = pilot.store.get(pid)
    rev = pilot.store.revision(pid, letter)
    filename, dxf = pilot.dxf(pid, letter)
    return {
        "event": "drawing.approved",
        "external_id": rec.integration.external_id if rec.integration else None,
        "project_id": pid,
        "project_name": rec.info.name,
        "drawing_number": rec.info.drawing_number,
        "revision": letter,
        "approved_by": rev.approved_by,
        "approved_at": rev.approved_at,
        "counts": rev.model.counts(),
        "warnings": len(rev.validation.warnings),
        "dxf": {"filename": filename, "content_type": "application/dxf", "size": len(dxf),
                "content_base64": base64.b64encode(dxf).decode(), "url": file_url(pid, letter, "dxf")},
        "svg_url": file_url(pid, letter, "svg"),
        "launch_url": launch_url(pid),
    }


def sign(body: bytes, timestamp: str) -> str:
    """X-CadPilot-Signature: sha256=HMAC(secret, "<timestamp>.<body>")."""
    return "sha256=" + hmac.new(_secret(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def _http_post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - URL checked to be http(s)
            return resp.status, resp.read(500).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(500).decode(errors="replace")


def send(pilot, pid: str, letter: str, post: Poster | None = None, backoff: float = 2.0) -> Delivery:
    """POST the approved drawing to the CRM's callback URL, retrying on failure. Does not save."""
    post = post or _http_post
    rec = pilot.store.get(pid)
    url = rec.integration.callback_url if rec.integration else None
    if not url:
        return Delivery(revision=letter, detail="No callback URL: the CRM fetches the drawing from the status endpoint")
    body = json.dumps(payload(pilot, pid, letter)).encode()
    last = Delivery(revision=letter)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        ts = str(int(time.time()))
        headers = {"Content-Type": "application/json", "User-Agent": "CadPilot-Webhook/1",
                   "X-CadPilot-Event": "drawing.approved", "X-CadPilot-Timestamp": ts, "X-CadPilot-Signature": sign(body, ts)}
        try:
            code, text = post(url, body, headers)
            last = Delivery(revision=letter, ok=200 <= code < 300, http_status=code, detail=text[:300])
        except Exception as exc:  # network error: retry
            last = Delivery(revision=letter, detail=f"{type(exc).__name__}: {exc}"[:300])
        if last.ok:
            break
        log.warning("CRM delivery of %s rev %s failed (attempt %d): %s", pid, letter, attempt, last.detail or last.http_status)
        if attempt < MAX_ATTEMPTS and backoff:
            time.sleep(backoff * attempt)
    last.detail = last.detail if not last.ok else f"Delivered to the CRM ({last.http_status})"
    return last


def record(pilot, pid: str, delivery: Delivery) -> None:
    rec = pilot.store.get(pid)
    if rec.integration is None:
        return
    rec.integration.deliveries.append(delivery)
    rec.integration.deliveries = rec.integration.deliveries[-20:]
    pilot.store.save(rec)
