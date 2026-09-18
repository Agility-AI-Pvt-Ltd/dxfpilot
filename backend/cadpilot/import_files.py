"""Copy projects from the file store (backend/storage) into PostgreSQL.

    uv run python -m cadpilot.import_files [storage_dir]

Safe to re-run: projects already in the database are skipped. The files are left in place.
"""

from __future__ import annotations

import os
import sys

from . import config  # noqa: F401  (loads backend/.env)
from .pg_store import PostgresStore
from .store import FileStore


def main() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set (backend/.env)")
    files = FileStore(sys.argv[1] if len(sys.argv) > 1 else None)
    pg = PostgresStore(url)
    imported = skipped = 0
    for summary in files.list_projects():
        rec = files.get(summary.id)
        revisions = []
        for letter in rec.revisions:
            svg_path = files.root / rec.info.id / "revisions" / f"{letter}.svg"
            revisions.append((files.revision(rec.info.id, letter), svg_path.read_text() if svg_path.exists() else ""))
        if pg.import_project(rec, files.source_files(rec.info.id), files.source_data(rec.info.id), revisions):
            imported += 1
            print(f"imported {rec.info.id}  {rec.info.name}  ({len(revisions)} revisions)")
        else:
            skipped += 1
    pg.close()
    print(f"done: {imported} imported, {skipped} already present")


if __name__ == "__main__":
    main()
