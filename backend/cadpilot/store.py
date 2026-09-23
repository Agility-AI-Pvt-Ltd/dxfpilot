"""Project persistence.

`ProjectStore` is the only persistence boundary. Two implementations:

- `PostgresStore` (cadpilot/pg_store.py) — used whenever DATABASE_URL is set.
- `FileStore` — one directory per project; a fallback for running without a database.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from . import config  # noqa: F401  (loads backend/.env)
from .agents.llm import LLMCall
from .engine.revisions import ModelDiff
from .engine.tags import TagRegistry
from .ingest.excel import SourceData, TableOverride
from .model.engineering import EngineeringModel, ProjectInfo
from .model.intent import ChangeRequest, DesignIntent
from .model.proposals import ValidationReport

log = logging.getLogger(__name__)

SOURCE_ROLES = ("mass_balance", "design_data")
SourceFiles = dict[str, tuple[str, bytes]]  # role -> (original filename, content)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    at: str = Field(default_factory=now_iso)
    revision: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Delivery(BaseModel):
    """One attempt to send an approved drawing back to the system the project came from."""

    revision: str
    at: str = Field(default_factory=now_iso)
    ok: bool = False
    http_status: int | None = None
    detail: str = ""


class Integration(BaseModel):
    """The external system (e.g. the CRM) that created this project and receives its approved drawings."""

    source: str = "crm"
    external_id: str
    callback_url: str | None = None  # approved DXF is POSTed here
    return_url: str | None = None  # "Back to CRM" link in the workspace
    request: str = ""  # first-draft instruction sent with the data
    created_at: str = Field(default_factory=now_iso)
    deliveries: list[Delivery] = Field(default_factory=list)


RevisionStatus = Literal["ready_for_review", "needs_attention", "approved", "superseded"]


class Revision(BaseModel):
    revision: str
    created_at: str = Field(default_factory=now_iso)
    status: RevisionStatus
    model: EngineeringModel
    intent: DesignIntent
    validation: ValidationReport
    events: list[dict[str, Any]] = Field(default_factory=list)
    change_request: ChangeRequest | None = None
    diff: ModelDiff | None = None
    diff_summary: str = ""
    approved_by: str | None = None
    approved_at: str | None = None


class ProjectRecord(BaseModel):
    info: ProjectInfo
    created_at: str = Field(default_factory=now_iso)
    sources: dict[str, str] = Field(default_factory=dict)  # role -> original filename
    intent: DesignIntent = Field(default_factory=DesignIntent)
    registry: TagRegistry = Field(default_factory=TagRegistry)
    revisions: list[str] = Field(default_factory=list)
    chat: list[ChatMessage] = Field(default_factory=list)
    corrections: list[ChangeRequest] = Field(default_factory=list)  # structured learning record
    table_overrides: list[TableOverride] = Field(default_factory=list)  # reviewer column mappings
    integration: Integration | None = None  # set when an external system (CRM) created the project

    @field_validator("sources", mode="before")
    @classmethod
    def _legacy_sources(cls, v: Any) -> Any:
        # projects created before role-based upload stored a plain list of filenames
        if isinstance(v, list):
            return {("mass_balance" if "mass" in n.lower() else "design_data"): n for n in v}
        return v

    @property
    def current(self) -> str | None:
        return self.revisions[-1] if self.revisions else None


class RevisionSummary(BaseModel):
    revision: str
    status: RevisionStatus
    created_at: str
    cause: str  # what produced it: the first request, a correction, or a restore
    diff_summary: str
    equipment: int
    lines: int
    instruments: int
    errors: int
    warnings: int
    approved_by: str | None = None

    @classmethod
    def of(cls, rev: "Revision") -> "RevisionSummary":
        c = rev.model.counts()
        return cls(
            revision=rev.revision, status=rev.status, created_at=rev.created_at,
            cause=rev.change_request.message if rev.change_request else (rev.intent.request or "First draft"),
            diff_summary=rev.diff_summary, equipment=c["equipment"], lines=c["lines"], instruments=c["instruments"],
            errors=len(rev.validation.errors), warnings=len(rev.validation.warnings), approved_by=rev.approved_by,
        )


class ProjectSummary(BaseModel):
    id: str
    name: str
    drawing_number: str = ""
    current: str | None
    created_at: str


def new_project(name: str) -> ProjectRecord:
    pid = uuid.uuid4().hex[:12]
    return ProjectRecord(info=ProjectInfo(id=pid, name=name, drawing_number=f"CP-PID-{pid[:4].upper()}-001"))


def check_id(pid: str) -> str:
    if not re.fullmatch(r"[a-z0-9-]{4,64}", pid):
        raise KeyError(pid)
    return pid


def check_letter(letter: str) -> str:
    if not re.fullmatch(r"[A-Z]{1,3}", letter):
        raise KeyError(letter)
    return letter


class ProjectStore(ABC):
    """Missing projects/revisions raise KeyError."""

    @abstractmethod
    def create(self, name: str) -> ProjectRecord: ...

    @abstractmethod
    def list_projects(self) -> list[ProjectSummary]: ...

    @abstractmethod
    def get(self, pid: str) -> ProjectRecord: ...

    @abstractmethod
    def save(self, rec: ProjectRecord) -> None:
        """Persist project fields; chat messages and corrections are append-only."""

    @abstractmethod
    def put_sources(self, pid: str, files: SourceFiles) -> None:
        """Store workbooks by role; a role that is re-uploaded replaces the old file."""

    @abstractmethod
    def source_files(self, pid: str) -> SourceFiles: ...

    @abstractmethod
    def save_source_data(self, pid: str, data: SourceData) -> None: ...

    @abstractmethod
    def source_data(self, pid: str) -> SourceData | None: ...

    @abstractmethod
    def commit_revision(self, rec: ProjectRecord, rev: Revision, svg: str, supersede: str | None) -> None:
        """Atomically: store the new revision, mark `supersede` superseded, save the project.
        The caller has already appended rev.revision to rec.revisions."""

    @abstractmethod
    def update_revision(self, pid: str, rev: Revision, svg: str) -> None:
        """Update status/approval (and the stamped drawing) of an existing revision."""

    @abstractmethod
    def revision(self, pid: str, letter: str) -> Revision: ...

    @abstractmethod
    def list_revisions(self, pid: str) -> list[RevisionSummary]:
        """Oldest first, without loading full models where the backend allows."""

    @abstractmethod
    def record_llm_calls(self, pid: str, revision: str | None, calls: list[LLMCall]) -> None:
        """Audit log of every LLM call (append-only)."""

    @abstractmethod
    def llm_calls(self, pid: str) -> list[dict[str, Any]]:
        """Oldest first; each row is an LLMCall plus the revision it contributed to (if any)."""

    def find_external(self, source: str, external_id: str) -> str | None:
        """Id of the project an external system created for its record, if any."""
        for s in self.list_projects():
            integ = self.get(s.id).integration
            if integ and integ.source == source and integ.external_id == external_id:
                return s.id
        return None

    def close(self) -> None:  # noqa: B027 - optional hook
        pass


class FileStore(ProjectStore):
    """One directory per project: project.json, source.json, sources/<role>.xlsx, revisions/<A>.json|svg."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or os.environ.get("CADPILOT_STORAGE") or Path(__file__).resolve().parents[1] / "storage")
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, pid: str) -> Path:
        return self.root / check_id(pid)

    def create(self, name: str) -> ProjectRecord:
        rec = new_project(name)
        (self._dir(rec.info.id) / "sources").mkdir(parents=True)
        (self._dir(rec.info.id) / "revisions").mkdir()
        self.save(rec)
        return rec

    def list_projects(self) -> list[ProjectSummary]:
        out = []
        for d in self.root.iterdir():
            if (d / "project.json").exists():
                r = ProjectRecord.model_validate_json((d / "project.json").read_text())
                out.append(ProjectSummary(id=r.info.id, name=r.info.name, drawing_number=r.info.drawing_number, current=r.current, created_at=r.created_at))
        return sorted(out, key=lambda r: r.created_at, reverse=True)

    def get(self, pid: str) -> ProjectRecord:
        path = self._dir(pid) / "project.json"
        if not path.exists():
            raise KeyError(pid)
        return ProjectRecord.model_validate_json(path.read_text())

    def save(self, rec: ProjectRecord) -> None:
        path = self._dir(rec.info.id) / "project.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(rec.model_dump_json(indent=1, by_alias=True))
        tmp.replace(path)

    def put_sources(self, pid: str, files: SourceFiles) -> None:
        rec = self.get(pid)
        self._migrate_legacy(pid, rec.sources)
        for role, (name, data) in files.items():
            if role not in SOURCE_ROLES:
                raise ValueError(f"Unknown source role {role!r}")
            (self._dir(pid) / "sources" / f"{role}.xlsx").write_bytes(data)
            rec.sources[role] = name
        self.save(rec)

    def _migrate_legacy(self, pid: str, sources: dict[str, str]) -> None:
        """Projects created before role-based upload kept files under their original names."""
        d = self._dir(pid) / "sources"
        for role, name in sources.items():
            legacy, target = d / name, d / f"{role}.xlsx"
            if role in SOURCE_ROLES and legacy.exists() and not target.exists() and legacy != target:
                legacy.rename(target)

    def source_files(self, pid: str) -> SourceFiles:
        rec = self.get(pid)
        self._migrate_legacy(pid, rec.sources)
        out: SourceFiles = {}
        for role in SOURCE_ROLES:
            p = self._dir(pid) / "sources" / f"{role}.xlsx"
            if p.exists():
                out[role] = (rec.sources.get(role, p.name), p.read_bytes())
        return out

    def save_source_data(self, pid: str, data: SourceData) -> None:
        (self._dir(pid) / "source.json").write_text(data.model_dump_json(indent=1))

    def source_data(self, pid: str) -> SourceData | None:
        p = self._dir(pid) / "source.json"
        return SourceData.model_validate_json(p.read_text()) if p.exists() else None

    def _write_revision(self, pid: str, rev: Revision, svg: str) -> None:
        d = self._dir(pid) / "revisions"
        (d / f"{rev.revision}.json").write_text(rev.model_dump_json(indent=1, by_alias=True))
        (d / f"{rev.revision}.svg").write_text(svg)

    def commit_revision(self, rec: ProjectRecord, rev: Revision, svg: str, supersede: str | None) -> None:
        self._write_revision(rec.info.id, rev, svg)
        if supersede:
            old = self.revision(rec.info.id, supersede)
            old.status = "superseded"
            path = self._dir(rec.info.id) / "revisions" / f"{supersede}.json"
            path.write_text(old.model_dump_json(indent=1, by_alias=True))
        self.save(rec)

    def update_revision(self, pid: str, rev: Revision, svg: str) -> None:
        self._write_revision(pid, rev, svg)

    def list_revisions(self, pid: str) -> list[RevisionSummary]:
        return [RevisionSummary.of(self.revision(pid, r)) for r in self.get(pid).revisions]

    def record_llm_calls(self, pid: str, revision: str | None, calls: list[LLMCall]) -> None:
        with (self._dir(pid) / "llm_calls.jsonl").open("a") as f:
            for c in calls:
                f.write(json.dumps({**c.model_dump(), "revision": revision}) + "\n")

    def llm_calls(self, pid: str) -> list[dict[str, Any]]:
        p = self._dir(pid) / "llm_calls.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def revision(self, pid: str, letter: str) -> Revision:
        p = self._dir(pid) / "revisions" / f"{check_letter(letter)}.json"
        if not p.exists():
            raise KeyError(letter)
        return Revision.model_validate_json(p.read_text())


def get_store() -> ProjectStore:
    """PostgreSQL when DATABASE_URL is set, otherwise the file store."""
    url = os.environ.get("DATABASE_URL")
    if url:
        from .pg_store import PostgresStore

        return PostgresStore(url)
    log.warning("DATABASE_URL is not set — using the file store at backend/storage")
    return FileStore()
