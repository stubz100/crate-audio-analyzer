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
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 9

# The third kind of segment (spec §6.4): one of the 10-s CLAP windows a file
# longer than the model's input is embedded through, kept with its vector so a
# search can land inside a long file. Written by the embedding stage, no
# strength, replaced whenever the parent is embedded again.
WINDOW_METHOD = "window"
# v1 = Phase 1: `samples`
# v2 = Phase 2: `analysis` + `classification`
# v3 = Phase 2 review: staleness timestamps (`samples.content_changed_at`,
#      `analysis.analyzed_at`); `classification` columns aligned with spec §8's
#      mirror table (`content_class`, `structural_type`, `confidence`)
# v4 = Phase 3: `segments` + `segment_analysis` / `segment_embedding` /
#      `segment_classification` (spec §6, §8)
# v5 = Phase 3 review: `segments.needs_review` (a manual segment whose parent's
#      content changed underneath it)
# v6 = Phase 4: `embedding` + `text_tags` (spec §8; nodes D, X, E)
# v7 = Phase 6: `map_layout` + `map_position` (spec §8; node G)
# v8 = CLAP windows (spec §6.4): `segments.detection_method` gains 'window' — a
#      rebuild of `segments`, since the value lives in a CHECK constraint
# v9 = `libraries` (spec §9.6): the folders the index knows, with their root and
#      in-scope flags — the Library panel's list, seeded from the index + settings


def scope_clause(
    scope: Iterable[str] | None, column: str = "s.filepath"
) -> tuple[str, list[str]]:
    """SQL restricting `column` (an absolute file path) to files under any
    folder in `scope` — the §9.6 folder-scope list that gates the expensive
    stages. No scope means no restriction (the CLI's behaviour), so this
    returns `("", [])`; otherwise an ` AND (...)` fragment plus its
    parameters, to append to a WHERE clause.

    Folders are resolved the way the scanner resolves its root, so a folder
    picked in a dialog matches what the scanner stored. `!` is the LIKE
    escape character because a backslash is every Windows path separator.
    """
    folders = [f for f in (scope or ()) if f]
    if not folders:
        return "", []
    params: list[str] = []
    for folder in folders:
        prefix = os.path.join(str(Path(folder).resolve()), "")
        escaped = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        params.append(escaped + "%")
    clause = " OR ".join(f"{column} LIKE ? ESCAPE '!'" for _ in params)
    return f" AND ({clause})", params


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
    detection_method  TEXT NOT NULL DEFAULT 'auto',   -- 'auto' | 'manual' (§6.2/§6.3) |
                                                      -- 'window' (§6.4: a CLAP window of a
                                                      -- long file, from the embedding stage)
    is_user_confirmed INTEGER NOT NULL DEFAULT 0,     -- manual save; blocks auto overwrite (§6.3)
    strength          REAL,                           -- onset strength, 0..1 of the strongest in
                                                      -- the parent; the cap's tie-break (§6.2)
    detected_at       TEXT,                           -- ISO-8601 UTC; stale when older than
                                                      -- samples.content_changed_at
    needs_review      INTEGER NOT NULL DEFAULT 0,     -- manual segment whose parent's content
                                                      -- changed and now ends past the file (§6.3:
                                                      -- bounds are never touched automatically,
                                                      -- so they are flagged instead)
    cache_path        TEXT,                           -- lazy render target (§6.5) — Phase 4.5/9
    cache_rendered_at TEXT,
    UNIQUE (sample_id, start_ms, end_ms, detection_method),
    CHECK (end_ms > start_ms),
    CHECK (detection_method IN ('auto', 'manual', 'window'))
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

CREATE TABLE IF NOT EXISTS segment_embedding (       -- Phase 4 (CLAP, windowed), node C2
    segment_id INTEGER NOT NULL
               REFERENCES segments(id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    vector     BLOB NOT NULL,                        -- float32, L2-normalised
    UNIQUE (segment_id, model_name)
    -- no timestamp: re-detection replaces auto segments (new ids), and a manual
    -- segment's row is deleted when its bounds or parent content change
);

-- Phase 4 (spec §8): node D. The conceptual similarity vector and the text-
-- search backbone. Map coordinates never live here (see map_layout, §8).
CREATE TABLE IF NOT EXISTS embedding (
    sample_id   INTEGER NOT NULL
                REFERENCES samples(id) ON DELETE CASCADE,
    model_name  TEXT NOT NULL,                       -- 'clap' (| 'qwen2audio-latent', §5.3)
    vector      BLOB NOT NULL,                       -- float32, L2-normalised
    embedded_at TEXT,                                -- stale when older than
                                                     -- samples.content_changed_at
    UNIQUE (sample_id, model_name)
);

-- Phase 4 (spec §8): MACHINE output only — the raw, regenerable layer. Nodes
-- X (zero-shot tags) and, later, X1 (captions). Accepting a chip promotes it
-- into the curated tags/sample_tags layer (Phase 7); these rows are disposable.
CREATE TABLE IF NOT EXISTS text_tags (
    id                INTEGER PRIMARY KEY,
    sample_id         INTEGER NOT NULL
                      REFERENCES samples(id) ON DELETE CASCADE,
    tag_or_caption    TEXT NOT NULL,
    source_model      TEXT NOT NULL,                 -- 'clap-zeroshot' | 'clap-class' |
                                                     -- 'qwen2audio-caption'
    score             REAL,                          -- zero-shot: cosine similarity;
                                                     -- clap-class: one row per class,
                                                     -- its softmax probability
    is_user_confirmed INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT,
    UNIQUE (sample_id, source_model, tag_or_caption)
);
CREATE INDEX IF NOT EXISTS idx_text_tags_sample ON text_tags(sample_id);

CREATE TABLE IF NOT EXISTS segment_classification (  -- Phase 4; structural_type is fixed
    segment_id        INTEGER NOT NULL UNIQUE
                      REFERENCES segments(id) ON DELETE CASCADE,
    content_class     TEXT,                          -- copied from the parent (§6.4)
    structural_type   TEXT NOT NULL DEFAULT 'one-shot',
    confidence        REAL,
    is_user_confirmed INTEGER NOT NULL DEFAULT 0
);

-- Phase 6 (spec §8): node G. One row per explicitly computed layout. The
-- coordinates are a function of weights + scope + fit, never of one
-- embedding model, which is why they live here and not on embedding rows.
CREATE TABLE IF NOT EXISTS map_layout (
    id                 INTEGER PRIMARY KEY,
    computed_at        TEXT NOT NULL,
    scope_description  TEXT,
    blend_weights_json TEXT,                          -- the per-axis weights the fit used
    umap_params_json   TEXT,
    random_seed        INTEGER,
    is_current         INTEGER NOT NULL DEFAULT 0,
    reducer            TEXT,                          -- 'umap' | 'pca' (no `map` extra)
    model_path         TEXT                           -- pickled reducer: the anchored-only transform
);

CREATE TABLE IF NOT EXISTS map_position (
    layout_id  INTEGER NOT NULL REFERENCES map_layout(id) ON DELETE CASCADE,
    sample_id  INTEGER REFERENCES samples(id) ON DELETE CASCADE,
    segment_id INTEGER REFERENCES segments(id) ON DELETE CASCADE,   -- badge / nested display
                                                                    -- only, never a point (§6.4)
    map_x      REAL NOT NULL,
    map_y      REAL NOT NULL,
    CHECK ((sample_id IS NULL) <> (segment_id IS NULL)),
    UNIQUE (layout_id, sample_id),
    UNIQUE (layout_id, segment_id)
);
CREATE INDEX IF NOT EXISTS idx_map_position_layout ON map_position(layout_id);

-- Phase 8 (spec §9.6, 2026-09-08): the folders the index knows — the Library
-- panel's list. `in_scope` = shown in the list and map, walked by Rescan,
-- covered by Recompute; 0 = dormant, rows kept untouched. `is_root` = at most
-- one, the library's home folder. Removing a folder deletes its samples.
CREATE TABLE IF NOT EXISTS libraries (
    id              INTEGER PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,            -- absolute, resolved, native separators
    is_root         INTEGER NOT NULL DEFAULT 0,
    in_scope        INTEGER NOT NULL DEFAULT 1,
    added_at        TEXT NOT NULL,
    last_scanned_at TEXT
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


def _migrate_v5(conn: sqlite3.Connection) -> None:
    _add_column(conn, "segments", "needs_review", "INTEGER NOT NULL DEFAULT 0")


def _segments_ddl(table: str) -> str:
    """SCHEMA's `segments` definition under another table name — the one
    source of truth for the table's shape, so a rebuild cannot drift."""
    start = SCHEMA.index("CREATE TABLE IF NOT EXISTS segments (")
    end = SCHEMA.index("\n);", start) + 3
    return SCHEMA[start:end].replace(
        "CREATE TABLE IF NOT EXISTS segments (", f"CREATE TABLE {table} (", 1
    )


def segments_accept_windows(conn: sqlite3.Connection) -> bool:
    """Whether `segments`' CHECK constraint lists 'window' — read from the
    stored DDL's constraint itself, not its comments."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'segments'"
    ).fetchone()
    if row is None:
        return False
    match = re.search(r"CHECK\s*\(\s*detection_method\s+IN\s*\(([^)]*)\)", row[0], re.IGNORECASE)
    return match is not None and "'window'" in match.group(1)


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """`segments.detection_method` gains 'window' (spec §6.4). The allowed
    values are a CHECK constraint, which SQLite cannot alter in place, so the
    table is rebuilt the documented way — create the new shape, copy, drop
    the old, rename — with foreign keys OFF for the duration: with them on,
    DROP TABLE would cascade through every segment_analysis /
    segment_embedding / segment_classification / map_position row."""
    if not _columns(conn, "segments") or segments_accept_windows(conn):
        return
    conn.commit()                                   # a PRAGMA inside a transaction is ignored
    conn.execute("PRAGMA foreign_keys = OFF")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
        raise RuntimeError("could not switch foreign keys off for the segments rebuild")
    try:
        conn.execute("BEGIN")
        conn.execute(_segments_ddl("segments_new"))
        new_columns = [r[1] for r in conn.execute("PRAGMA table_info(segments_new)")]
        old_columns = _columns(conn, "segments")
        columns = ", ".join(c for c in new_columns if c in old_columns)
        conn.execute(f"INSERT INTO segments_new ({columns}) SELECT {columns} FROM segments")
        conn.execute("DROP TABLE segments")
        conn.execute("ALTER TABLE segments_new RENAME TO segments")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_segments_sample ON segments(sample_id)")
        dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
        if dangling:
            raise RuntimeError(f"segments rebuild left {len(dangling)} dangling foreign keys")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


_MIGRATIONS: dict[int, list] = {
    2: [],              # v1 -> v2: new tables only; SCHEMA's CREATE IF NOT EXISTS covers it
    3: [_migrate_v3],   # v2 -> v3: staleness timestamps + spec §8 classification columns
    4: [_migrate_v4],   # v3 -> v4: segment tables + samples.segments_detected_at
    5: [_migrate_v5],   # v4 -> v5: segments.needs_review
    6: [],              # v5 -> v6: embedding + text_tags; CREATE IF NOT EXISTS covers it
    7: [],              # v6 -> v7: map_layout + map_position; CREATE IF NOT EXISTS covers it
    8: [_migrate_v8],   # v7 -> v8: segments.detection_method accepts 'window' (table rebuild)
    9: [],              # v8 -> v9: libraries; CREATE IF NOT EXISTS covers it, the panel seeds it
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
    # A GUI will read this index while a CLI run writes to it for hours (spec
    # §9.6 recompute actions). WAL lets readers proceed during a writer's
    # commit, and the busy timeout turns the few remaining lock collisions
    # into short waits instead of "database is locked" errors (2026-09-06
    # review). In-memory databases report 'memory' here, which is fine.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
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
