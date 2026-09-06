"""SQLite schema and connection handling for the Crate index.

Phase 1 scope (spec §12): the `samples` table in its full spec §8 shape.
Later phases populate the segment_* columns; every other table arrives with
the phase that needs it. Timestamps are ISO-8601 UTC strings.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1  # bump + migrate here when a later phase reshapes the schema


def default_db_path() -> Path:
    """Stable per-user index location (Windows-only project, spec §3).

    A cwd-relative default silently created a second empty index when the
    CLI ran from another folder (2026-09-06 review); LOCALAPPDATA is stable
    regardless of invocation directory. Reverses the 2026-09-05 repo-local
    decision — see journal.
    """
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Crate" / "crate.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (              -- audio files only (spec §8)
    id                       INTEGER PRIMARY KEY,
    filepath                 TEXT NOT NULL UNIQUE, -- absolute path, native separators
    filename                 TEXT NOT NULL,
    folder                   TEXT NOT NULL DEFAULT '',
                             -- POSIX-style path relative to the scan root; '' = root
    duration_s               REAL,                 -- NULL = header unreadable (logged at scan)
    sample_rate              INTEGER,
    channels                 INTEGER,
    added_at                 TEXT NOT NULL,        -- ISO-8601 UTC, first scan that saw the file
    last_scanned_at          TEXT NOT NULL,        -- ISO-8601 UTC, last scan that saw the file
    file_size                INTEGER NOT NULL,     -- cheap change key (with file_mtime)
    file_mtime               REAL NOT NULL,        -- re-scan diff compares these first (spec §8)
    file_hash                TEXT,                 -- blake2b of content; computed ONLY when
                                                   -- size/mtime changed (spec §8)
    segment_candidates_found INTEGER,              -- Phase 3
    segments_capped          INTEGER,              -- Phase 3
    effective_sensitivity    REAL                  -- Phase 3
);
"""


def open_db(db_path: Path | str) -> sqlite3.Connection:
    """Open the index database at `db_path`, creating it (and its parent
    directory) if needed, and ensure the schema exists. Idempotent.

    Schema versioning (2026-09-06 review): `PRAGMA user_version` is the
    migration anchor for later phases — a DB written by a newer build is
    refused loudly instead of failing deep in a query.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"index at {db_path} uses schema v{version}; "
            f"this build understands up to v{SCHEMA_VERSION}"
        )
    conn.executescript(SCHEMA)
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn