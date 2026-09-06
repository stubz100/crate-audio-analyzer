"""SQLite schema and connection handling for the Crate index.

Tables arrive with the phase that needs them (spec §12): `samples` in
Phase 1, `analysis` + `classification` in Phase 2. Timestamps are ISO-8601
UTC strings (`now_iso()`), microsecond resolution so that "content changed
after it was analyzed" is decidable even within one second.

Schema changes are versioned with `PRAGMA user_version` (2026-09-06 review)
and applied by `_MIGRATIONS`. Every step is idempotent — it inspects the
table's current shape first — so a DB of any earlier version (including the
unversioned Phase 1 build) upgrades in one `open_db` call, and a fresh DB,
created in its final shape by `SCHEMA`, passes through every step as a no-op.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 4
# v1 = Phase 1: `samples`
# v2 = Phase 2: `analysis` + `classification`
# v3 = Phase 2 review: staleness timestamps (`samples.content_changed_at`,
#      `analysis.analyzed_at`); `classification` columns aligned with spec §8's
#      mirror table (`content_class`, `structural_type`, `confidence`)
# v4 = Phase 3: `segments` + `segment_analysis` / `segment_embedding` /
#      `segment_classification` (spec §6, §8)


def now_iso() -> str:
    """Current UTC time as ISO-8601 with microseconds — the shared timestamp
    format for every table, so string comparison orders correctly."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
    content_changed_at       TEXT,                 -- ISO-8601 UTC of the scan that first saw
                                                   -- this content (insert or genuine change,
                                                   -- never a hash-identical retouch). Derived
                                                   -- rows older than this are STALE (v3)
    segment_candidates_found INTEGER,              -- Phase 3: segments the detector proposed
                                                   -- after the length rules, before the cap;
                                                   -- NULL = never segmented
    segments_capped          INTEGER,              -- Phase 3: 1 when the cap actually bound,
                                                   -- so the UI can show the §6.2 warning
    effective_sensitivity    REAL,                 -- Phase 3: strength of the weakest KEPT
                                                   -- transient when capped (§6.2 "sensitivity
                                                   -- was raised to fit")
    segments_detected_at     TEXT                  -- Phase 3: ISO-8601 UTC; stale when older
                                                   -- than content_changed_at. Separate from the
                                                   -- counters because a sample can legitimately
                                                   -- be segmented and yield zero segments
);

CREATE TABLE IF NOT EXISTS analysis (              -- samples only (spec §8), Phase 2
    sample_id              INTEGER NOT NULL UNIQUE
                           REFERENCES samples(id) ON DELETE CASCADE,
    analyzed_at            TEXT,                   -- ISO-8601 UTC; stale when older than
                                                   -- samples.content_changed_at (v3)
    tempo_bpm              REAL,                   -- loops only; NULL unless the tempo is
                                                   -- explicitly recognisable (ACID beats, or
                                                   -- acoustic periodicity that passed the loop
                                                   -- rule) — 2026-09-06 decision
    tempo_confidence       REAL,                   -- onset-autocorrelation peak, 0..1 (always
                                                   -- acoustic, even when tempo is from ACID)
    onset_count            INTEGER,                -- DOMINANT onsets (spec §4), see analysis.py
    is_loop                INTEGER NOT NULL DEFAULT 0,  -- embedded metadata and/or
                                                         -- periodic onsets + tempo (§4 Loop)
    key                    TEXT,                   -- pitch class from embedded root note
                                                   -- (ACID root_note when root_set); key
                                                   -- *estimation* is Phase 13 stretch
    harmonic_ratio         REAL,                   -- HPSS harmonic-energy share (§5.1 pitch gate)
    embedded_metadata_json TEXT,                   -- smpl/ACID chunks (spec §10), JSON
    peak_db                REAL,
    rms_db                 REAL,
    crest_factor           REAL,                   -- linear peak/RMS ratio
    attack_ms              REAL,                   -- frame-envelope rise to the first onset's
                                                   -- peak (§5.1); see analysis._envelope_times
    decay_ms               REAL,                   -- that peak to -20 dB on the frame envelope
    f0_hz                  REAL,                   -- NULL below the pitch gate (§5.1)
    pitch_confidence       REAL,                   -- voiced-frame fraction (yin)
    mfcc_mean              TEXT,                   -- JSON array[13]: per-coefficient MFCC
                                                   -- means. A scalar here would collapse the
                                                   -- whole Timbre axis (§5.1) to one number.
    mfcc_var               TEXT,                   -- JSON array[13]: per-coefficient variance
    spectral_contrast      TEXT,                   -- JSON array[7]: per-band frame means
    spectral_centroid      REAL,
    spectral_bandwidth     REAL,
    spectral_rolloff       REAL,
    spectral_flatness      REAL
);

CREATE TABLE IF NOT EXISTS classification (        -- samples only (spec §8), Phase 2+
    sample_id         INTEGER NOT NULL UNIQUE
                      REFERENCES samples(id) ON DELETE CASCADE,
    content_class     TEXT,                        -- Facet A (spec §4) — Phase 4 (CLAP + node E)
    structural_type   TEXT,                        -- Facet B (spec §4) — Phase 2 rules
    confidence        REAL,                        -- node E's flag-threshold input — Phase 4
    provenance        TEXT NOT NULL DEFAULT 'automatic',  -- automatic | manual (spec §11)
    source_model      TEXT,
    is_user_confirmed INTEGER NOT NULL DEFAULT 0   -- protection flag (spec §11)
);

-- Phase 3 (spec §6): a segment is an INDEX INTO a sample — a start/end marker
-- pair — never a row in `samples`. Every sample-facing query (map, list,
-- filters) can ignore these tables entirely (§6.4).
CREATE TABLE IF NOT EXISTS segments (
    id                INTEGER PRIMARY KEY,
    sample_id         INTEGER NOT NULL
                      REFERENCES samples(id) ON DELETE CASCADE,
    start_ms          INTEGER NOT NULL,
    end_ms            INTEGER NOT NULL,
    detection_method  TEXT NOT NULL DEFAULT 'auto',   -- 'auto' | 'manual' (§6.2/§6.3)
    is_user_confirmed INTEGER NOT NULL DEFAULT 0,     -- manual save; blocks auto overwrite (§6.3)
    strength          REAL,                           -- onset strength, 0..1 of the strongest in
                                                      -- the parent; the cap's tie-break (§6.2)
    detected_at       TEXT,                           -- ISO-8601 UTC; stale when older than
                                                      -- samples.content_changed_at
    cache_path        TEXT,                           -- lazy render target (§6.5) — Phase 4.5/9
    cache_rendered_at TEXT,
    UNIQUE (sample_id, start_ms, end_ms, detection_method),
    CHECK (end_ms > start_ms),
    CHECK (detection_method IN ('auto', 'manual'))
);
CREATE INDEX IF NOT EXISTS idx_segments_sample ON segments(sample_id);

-- Mirrors `analysis`, segment-scoped: no is_loop/key/embedded_metadata_json
-- (a segment has no metadata chunk and is a one-shot by construction, §6.4).
CREATE TABLE IF NOT EXISTS segment_analysis (
    segment_id         INTEGER NOT NULL UNIQUE
                       REFERENCES segments(id) ON DELETE CASCADE,
    analyzed_at        TEXT,
    tempo_bpm          REAL,
    onset_count        INTEGER,
    harmonic_ratio     REAL,
    peak_db            REAL,
    rms_db             REAL,
    crest_factor       REAL,
    attack_ms          REAL,
    decay_ms           REAL,
    f0_hz              REAL,
    pitch_confidence   REAL,
    mfcc_mean          TEXT,                          -- JSON array[13]
    mfcc_var           TEXT,                          -- JSON array[13]
    spectral_contrast  TEXT,                          -- JSON array[7]
    spectral_centroid  REAL,
    spectral_bandwidth REAL,
    spectral_rolloff   REAL,
    spectral_flatness  REAL
);

CREATE TABLE IF NOT EXISTS segment_embedding (       -- Phase 4 (CLAP, windowed)
    segment_id INTEGER NOT NULL
               REFERENCES segments(id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    vector     BLOB NOT NULL,
    UNIQUE (segment_id, model_name)
);

CREATE TABLE IF NOT EXISTS segment_classification (  -- Phase 4; structural_type is fixed
    segment_id        INTEGER NOT NULL UNIQUE
                      REFERENCES segments(id) ON DELETE CASCADE,
    content_class     TEXT,                          -- copied from the parent (§6.4)
    structural_type   TEXT NOT NULL DEFAULT 'one-shot',
    confidence        REAL,
    is_user_confirmed INTEGER NOT NULL DEFAULT 0
);
"""


# --- migrations -------------------------------------------------------------
# Each step must be safe to run against a table in EITHER shape (old, or already
# created in the final shape by SCHEMA), and against a table that does not
# exist yet — hence the PRAGMA table_info checks.


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = _columns(conn, table)
    if cols and column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _rename_column(conn: sqlite3.Connection, table: str, old: str, new: str) -> None:
    cols = _columns(conn, table)
    if old in cols and new not in cols:
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    _add_column(conn, "samples", "content_changed_at", "TEXT")
    conn.execute(
        "UPDATE samples SET content_changed_at = added_at "
        "WHERE content_changed_at IS NULL"
    )
    _add_column(conn, "analysis", "analyzed_at", "TEXT")
    _rename_column(conn, "classification", "facet_a", "content_class")
    _rename_column(conn, "classification", "facet_b", "structural_type")
    _add_column(conn, "classification", "confidence", "REAL")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    # The segment tables themselves are new, so SCHEMA's CREATE IF NOT EXISTS
    # covers them; only the parent-side timestamp needs an ALTER.
    _add_column(conn, "samples", "segments_detected_at", "TEXT")


_MIGRATIONS: dict[int, list] = {
    2: [],              # v1 -> v2: new tables only; SCHEMA's CREATE IF NOT EXISTS covers it
    3: [_migrate_v3],   # v2 -> v3: staleness timestamps + spec §8 classification columns
    4: [_migrate_v4],   # v3 -> v4: segment tables + samples.segments_detected_at
}


def open_db(db_path: Path | str) -> sqlite3.Connection:
    """Open the index database at `db_path`, creating it (and its parent
    directory) if needed, ensure the schema exists, and upgrade an older
    schema in place. Idempotent.

    A DB written by a newer build is refused loudly instead of failing deep
    in a query (2026-09-06 review).
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
    for target in range(version + 1, SCHEMA_VERSION + 1):
        for step in _MIGRATIONS.get(target, ()):
            step(conn)
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn
