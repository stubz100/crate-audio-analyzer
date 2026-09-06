"""SQLite schema and connection handling for the Crate index.

Phase 1 scope (spec §12): the `samples` table in its full spec §8 shape.
Later phases populate the segment_* columns; every other table arrives with
the phase that needs it. Timestamps are ISO-8601 UTC strings.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: Default index location, relative to the working directory (spec §15 /
#: journal decision): repo-local, already anticipated by .gitignore.
DEFAULT_DB_RELPATH = Path(".crate_cache") / "crate.db"

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
    directory) if needed, and ensure the schema exists. Idempotent."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn