"""PostgreSQL project store.

Tables
  projects        one row per project: info, design intent, tag registry
  source_files    uploaded workbooks (bytea) by role, with sha256
  source_data     parsed workbook content used by the agents
  revisions       every committed version: model, intent, validation, run log, change request,
                  diff, rendered SVG, approval — plus summary columns for querying
  chat_messages   the review conversation (append-only)
  corrections     structured reviewer corrections (append-only learning record)

The schema is created/upgraded on startup from MIGRATIONS (tracked in schema_migrations,
serialised with an advisory lock so several API workers can start at once).
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .ingest.excel import SourceData
from .model.engineering import ProjectInfo
from .model.intent import ChangeRequest, DesignIntent
from .engine.tags import TagRegistry
from .store import (
    ChatMessage,
    ProjectRecord,
    ProjectStore,
    ProjectSummary,
    Revision,
    RevisionSummary,
    SOURCE_ROLES,
    SourceFiles,
    check_id,
    check_letter,
    new_project,
)

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE projects (
            id              text PRIMARY KEY,
            name            text NOT NULL,
            info            jsonb NOT NULL,
            intent          jsonb NOT NULL,
            tag_registry    jsonb NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE source_files (
            project_id      text NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            role            text NOT NULL CHECK (role IN ('mass_balance', 'design_data')),
            filename        text NOT NULL,
            content         bytea NOT NULL,
            sha256          text NOT NULL,
            uploaded_at     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (project_id, role)
        );

        CREATE TABLE source_data (
            project_id      text PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            data            jsonb NOT NULL,
            parsed_at       timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE revisions (
            project_id      text NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            revision        text NOT NULL,
            seq             integer NOT NULL,
            status          text NOT NULL CHECK (status IN ('ready_for_review', 'needs_attention', 'approved', 'superseded')),
            created_at      timestamptz NOT NULL,
            model           jsonb NOT NULL,
            intent          jsonb NOT NULL,
            validation      jsonb NOT NULL,
            events          jsonb NOT NULL,
            change_request  jsonb,
            diff            jsonb,
            diff_summary    text NOT NULL DEFAULT '',
            svg             text NOT NULL,
            approved_by     text,
            approved_at     timestamptz,
            equipment_count  integer NOT NULL,
            line_count       integer NOT NULL,
            instrument_count integer NOT NULL,
            error_count      integer NOT NULL,
            warning_count    integer NOT NULL,
            PRIMARY KEY (project_id, revision),
            UNIQUE (project_id, seq)
        );

        CREATE TABLE chat_messages (
            id              bigserial PRIMARY KEY,
            project_id      text NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            seq             integer NOT NULL,
            role            text NOT NULL CHECK (role IN ('user', 'assistant')),
            text            text NOT NULL,
            revision        text,
            data            jsonb NOT NULL DEFAULT '{}',
            created_at      timestamptz NOT NULL,
            UNIQUE (project_id, seq)
        );

        CREATE TABLE corrections (
            id              bigserial PRIMARY KEY,
            project_id      text NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            seq             integer NOT NULL,
            message         text NOT NULL,
            operations      jsonb NOT NULL,
            clarification   text,
            interpreter     text NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now(),
            UNIQUE (project_id, seq)
        );
        CREATE INDEX corrections_operations_gin ON corrections USING gin (operations);
        """,
    ),
    (
        2,
        """
        CREATE TABLE llm_calls (
            id                bigserial PRIMARY KEY,
            project_id        text NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            revision          text,
            purpose           text NOT NULL,
            model             text NOT NULL,
            served_model      text,
            outcome           text NOT NULL,
            latency_ms        integer NOT NULL,
            prompt_tokens     integer,
            completion_tokens integer,
            detail            text NOT NULL DEFAULT '',
            created_at        timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX llm_calls_project ON llm_calls (project_id, id);
        """,
    ),
]


def _j(model: Any) -> Jsonb:
    """pydantic model / plain data → Jsonb, keeping field aliases (e.g. Line.from)."""
    if hasattr(model, "model_dump"):
        return Jsonb(model.model_dump(mode="json", by_alias=True))
    return Jsonb(model)


def _iso(v: datetime | str | None) -> str | None:
    return v.isoformat(timespec="seconds") if isinstance(v, datetime) else v


class PostgresStore(ProjectStore):
    def __init__(self, url: str, schema: str | None = None, min_size: int = 1, max_size: int = 10):
        self.schema = schema

        def configure(conn: Connection) -> None:
            if schema:
                conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                conn.commit()

        self.pool = ConnectionPool(
            url, min_size=min_size, max_size=max_size, configure=configure,
            kwargs={"row_factory": dict_row}, open=True,
        )
        self.migrate()

    # ---- infrastructure -----------------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[Connection]:
        with self.pool.connection() as conn, conn.transaction():
            yield conn

    def migrate(self) -> None:
        with self.tx() as conn:
            if self.schema:
                conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
                conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema)))
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('cadpilot-migrations'))")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
            for version, ddl in MIGRATIONS:
                if version not in done:
                    conn.execute(ddl)
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))

    def close(self) -> None:
        self.pool.close()

    # ---- projects -----------------------------------------------------------------------------
    def create(self, name: str) -> ProjectRecord:
        rec = new_project(name)
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, info, intent, tag_registry, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (rec.info.id, rec.info.name, _j(rec.info), _j(rec.intent), _j(rec.registry), rec.created_at),
            )
        return rec

    def list_projects(self) -> list[ProjectSummary]:
        with self.tx() as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.name, p.created_at, p.info->>'drawing_number' AS drawing_number,
                       (SELECT r.revision FROM revisions r WHERE r.project_id = p.id ORDER BY r.seq DESC LIMIT 1) AS current
                FROM projects p ORDER BY p.created_at DESC
                """
            ).fetchall()
        return [
            ProjectSummary(id=r["id"], name=r["name"], drawing_number=r["drawing_number"] or "", current=r["current"], created_at=_iso(r["created_at"]))
            for r in rows
        ]

    def get(self, pid: str) -> ProjectRecord:
        check_id(pid)
        with self.tx() as conn:
            p = conn.execute("SELECT * FROM projects WHERE id = %s", (pid,)).fetchone()
            if p is None:
                raise KeyError(pid)
            revs = [r["revision"] for r in conn.execute("SELECT revision FROM revisions WHERE project_id = %s ORDER BY seq", (pid,))]
            files = {r["role"]: r["filename"] for r in conn.execute("SELECT role, filename FROM source_files WHERE project_id = %s", (pid,))}
            chat = conn.execute("SELECT * FROM chat_messages WHERE project_id = %s ORDER BY seq", (pid,)).fetchall()
            corr = conn.execute("SELECT * FROM corrections WHERE project_id = %s ORDER BY seq", (pid,)).fetchall()
        return ProjectRecord(
            info=ProjectInfo.model_validate(p["info"]),
            created_at=_iso(p["created_at"]),
            sources=files,
            intent=DesignIntent.model_validate(p["intent"]),
            registry=TagRegistry.model_validate(p["tag_registry"]),
            revisions=revs,
            chat=[
                ChatMessage(role=m["role"], text=m["text"], at=_iso(m["created_at"]), revision=m["revision"], data=m["data"])
                for m in chat
            ],
            corrections=[
                ChangeRequest.model_validate(
                    {"message": c["message"], "operations": c["operations"], "clarification": c["clarification"], "interpreter": c["interpreter"]}
                )
                for c in corr
            ],
        )

    def _save(self, conn: Connection, rec: ProjectRecord) -> None:
        pid = rec.info.id
        cur = conn.execute(
            "UPDATE projects SET name = %s, info = %s, intent = %s, tag_registry = %s, updated_at = now() WHERE id = %s",
            (rec.info.name, _j(rec.info), _j(rec.intent), _j(rec.registry), pid),
        )
        if cur.rowcount == 0:
            raise KeyError(pid)
        # chat and corrections are append-only: insert whatever the record has beyond what is stored
        n_chat = conn.execute("SELECT count(*) AS n FROM chat_messages WHERE project_id = %s", (pid,)).fetchone()["n"]
        for seq, m in enumerate(rec.chat[n_chat:], start=n_chat):
            conn.execute(
                "INSERT INTO chat_messages (project_id, seq, role, text, revision, data, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (pid, seq, m.role, m.text, m.revision, Jsonb(json.loads(json.dumps(m.data, default=str))), m.at),
            )
        n_corr = conn.execute("SELECT count(*) AS n FROM corrections WHERE project_id = %s", (pid,)).fetchone()["n"]
        for seq, c in enumerate(rec.corrections[n_corr:], start=n_corr):
            conn.execute(
                "INSERT INTO corrections (project_id, seq, message, operations, clarification, interpreter) VALUES (%s, %s, %s, %s, %s, %s)",
                (pid, seq, c.message, Jsonb([op.model_dump(mode="json") for op in c.operations]), c.clarification, c.interpreter),
            )

    def save(self, rec: ProjectRecord) -> None:
        with self.tx() as conn:
            self._save(conn, rec)

    def import_project(self, rec: ProjectRecord, files: SourceFiles, data: SourceData | None, revisions: list[tuple[Revision, str]]) -> bool:
        """Copy a whole project (e.g. from the file store) in one transaction. False if it already exists."""
        with self.tx() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE id = %s", (rec.info.id,)).fetchone():
                return False
            conn.execute(
                "INSERT INTO projects (id, name, info, intent, tag_registry, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (rec.info.id, rec.info.name, _j(rec.info), _j(rec.intent), _j(rec.registry), rec.created_at),
            )
        self.put_sources(rec.info.id, files)
        if data:
            self.save_source_data(rec.info.id, data)
        for rev, svg in revisions:
            self.commit_revision(rec, rev, svg, supersede=None)  # statuses are copied as they were
        return True

    # ---- sources ------------------------------------------------------------------------------
    def put_sources(self, pid: str, files: SourceFiles) -> None:
        check_id(pid)
        with self.tx() as conn:
            for role, (name, data) in files.items():
                if role not in SOURCE_ROLES:
                    raise ValueError(f"Unknown source role {role!r}")
                conn.execute(
                    """
                    INSERT INTO source_files (project_id, role, filename, content, sha256) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, role) DO UPDATE
                    SET filename = EXCLUDED.filename, content = EXCLUDED.content, sha256 = EXCLUDED.sha256, uploaded_at = now()
                    """,
                    (pid, role, name, data, hashlib.sha256(data).hexdigest()),
                )

    def source_files(self, pid: str) -> SourceFiles:
        with self.tx() as conn:
            rows = conn.execute("SELECT role, filename, content FROM source_files WHERE project_id = %s", (check_id(pid),))
            return {r["role"]: (r["filename"], bytes(r["content"])) for r in rows}

    def save_source_data(self, pid: str, data: SourceData) -> None:
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO source_data (project_id, data) VALUES (%s, %s)
                ON CONFLICT (project_id) DO UPDATE SET data = EXCLUDED.data, parsed_at = now()
                """,
                (check_id(pid), _j(data)),
            )

    def source_data(self, pid: str) -> SourceData | None:
        with self.tx() as conn:
            row = conn.execute("SELECT data FROM source_data WHERE project_id = %s", (check_id(pid),)).fetchone()
        return SourceData.model_validate(row["data"]) if row else None

    # ---- revisions ----------------------------------------------------------------------------
    def commit_revision(self, rec: ProjectRecord, rev: Revision, svg: str, supersede: str | None) -> None:
        pid = rec.info.id
        c = rev.model.counts()
        with self.tx() as conn:
            conn.execute("SELECT 1 FROM projects WHERE id = %s FOR UPDATE", (pid,))
            seq = conn.execute("SELECT coalesce(max(seq), 0) + 1 AS n FROM revisions WHERE project_id = %s", (pid,)).fetchone()["n"]
            conn.execute(
                """
                INSERT INTO revisions (project_id, revision, seq, status, created_at, model, intent, validation, events,
                    change_request, diff, diff_summary, svg, approved_by, approved_at,
                    equipment_count, line_count, instrument_count, error_count, warning_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    pid, rev.revision, seq, rev.status, rev.created_at, _j(rev.model), _j(rev.intent), _j(rev.validation),
                    Jsonb(json.loads(json.dumps(rev.events, default=str))),
                    _j(rev.change_request) if rev.change_request else None,
                    _j(rev.diff) if rev.diff else None,
                    rev.diff_summary, svg, rev.approved_by, rev.approved_at,
                    c["equipment"], c["lines"], c["instruments"], len(rev.validation.errors), len(rev.validation.warnings),
                ),
            )
            if supersede:
                conn.execute(
                    "UPDATE revisions SET status = 'superseded' WHERE project_id = %s AND revision = %s AND status <> 'approved'",
                    (pid, supersede),
                )
            self._save(conn, rec)

    def update_revision(self, pid: str, rev: Revision, svg: str) -> None:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE revisions SET status = %s, approved_by = %s, approved_at = %s, svg = %s WHERE project_id = %s AND revision = %s",
                (rev.status, rev.approved_by, rev.approved_at, svg, check_id(pid), check_letter(rev.revision)),
            )
            if cur.rowcount == 0:
                raise KeyError(rev.revision)

    def list_revisions(self, pid: str) -> list[RevisionSummary]:
        with self.tx() as conn:
            rows = conn.execute(
                """SELECT revision, status, created_at, diff_summary, equipment_count, line_count, instrument_count,
                          error_count, warning_count, approved_by,
                          coalesce(change_request->>'message', nullif(intent->>'request', ''), 'First draft') AS cause
                   FROM revisions WHERE project_id = %s ORDER BY seq""",
                (check_id(pid),),
            ).fetchall()
        return [
            RevisionSummary(
                revision=r["revision"], status=r["status"], created_at=_iso(r["created_at"]), cause=r["cause"],
                diff_summary=r["diff_summary"], equipment=r["equipment_count"], lines=r["line_count"],
                instruments=r["instrument_count"], errors=r["error_count"], warnings=r["warning_count"],
                approved_by=r["approved_by"],
            )
            for r in rows
        ]

    def record_llm_calls(self, pid: str, revision: str | None, calls: list) -> None:
        if not calls:
            return
        with self.tx() as conn:
            for c in calls:
                conn.execute(
                    """INSERT INTO llm_calls (project_id, revision, purpose, model, served_model, outcome, latency_ms,
                           prompt_tokens, completion_tokens, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (check_id(pid), revision, c.purpose, c.model, c.served_model, c.outcome, c.latency_ms,
                     c.prompt_tokens, c.completion_tokens, c.detail),
                )

    def llm_calls(self, pid: str) -> list[dict[str, Any]]:
        with self.tx() as conn:
            rows = conn.execute(
                """SELECT purpose, model, served_model, outcome, latency_ms, prompt_tokens, completion_tokens, detail,
                          revision, created_at FROM llm_calls WHERE project_id = %s ORDER BY id""",
                (check_id(pid),),
            ).fetchall()
        return [{**r, "at": _iso(r.pop("created_at"))} for r in rows]

    def revision(self, pid: str, letter: str) -> Revision:
        with self.tx() as conn:
            r = conn.execute(
                "SELECT * FROM revisions WHERE project_id = %s AND revision = %s", (check_id(pid), check_letter(letter))
            ).fetchone()
        if r is None:
            raise KeyError(letter)
        return Revision.model_validate(
            {
                "revision": r["revision"],
                "created_at": _iso(r["created_at"]),
                "status": r["status"],
                "model": r["model"],
                "intent": r["intent"],
                "validation": r["validation"],
                "events": r["events"],
                "change_request": r["change_request"],
                "diff": r["diff"],
                "diff_summary": r["diff_summary"],
                "approved_by": r["approved_by"],
                "approved_at": _iso(r["approved_at"]),
            }
        )
