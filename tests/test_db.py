"""Tests for the SQLite schema layer (spec §8)."""

from __future__ import annotations

import sqlite3

import pytest

from crate.db import SCHEMA, SCHEMA_VERSION, ids_autoincrement, ids_clause, open_db, segments_accept_windows


def test_open_db_creates_samples_table(tmp_path):
    db_path = tmp_path / "nested" / "crate.db"
    conn = open_db(db_path)
    try:
        assert db_path.exists()  # parent dirs created
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "samples" in tables
        columns = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
        # full spec §8 shape, including the Phase 3 columns created up front
        assert {
            "id", "filepath", "filename", "folder", "duration_s", "sample_rate",
            "channels", "added_at", "last_scanned_at", "file_size", "file_mtime",
            "file_hash", "segment_candidates_found", "segments_capped",
            "effective_sensitivity",
        } <= columns
    finally:
        conn.close()


def test_schema_is_idempotent(tmp_path):
    db_path = tmp_path / "crate.db"
    conn = open_db(db_path)
    conn.close()
    conn = open_db(db_path)  # second open must not fail ("IF NOT EXISTS")
    try:
        conn.executescript(SCHEMA)  # re-applying raw schema is also safe
    finally:
        conn.close()


def test_filepath_is_unique(tmp_path):
    conn = open_db(tmp_path / "crate.db")
    try:
        row = (
            r"C:\lib\a.wav", "a.wav", "", 1.0, 44100, 2,
            "2026-09-05T00:00:00+00:00", "2026-09-05T00:00:00+00:00", 100, 1.0,
        )
        conn.execute(
            """
            INSERT INTO samples
                (filepath, filename, folder, duration_s, sample_rate, channels,
                 added_at, last_scanned_at, file_size, file_mtime)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        try:
            conn.execute(
                """
                INSERT INTO samples
                    (filepath, filename, folder, duration_s, sample_rate,
                     channels, added_at, last_scanned_at, file_size, file_mtime)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate filepath was accepted")
    finally:
        conn.close()


def test_schema_version_is_stamped(tmp_path):
    # Finding 5: PRAGMA user_version anchors future migrations.
    conn = open_db(tmp_path / "crate.db")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version == SCHEMA_VERSION


def test_newer_schema_is_refused(tmp_path):
    # Finding 5: a DB written by a newer build must fail loudly, not corrupt.
    import crate.db as db_mod

    db_path = tmp_path / "crate.db"
    conn = open_db(db_path)
    conn.close()
    raw = sqlite3.connect(db_path)
    raw.execute(f"PRAGMA user_version = {db_mod.SCHEMA_VERSION + 98}")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError):
        open_db(db_path)


# --- Migrations (schema v3, 2026-09-06 Phase 2 review) ---

_V2_SHAPE = """
CREATE TABLE samples (
    id INTEGER PRIMARY KEY, filepath TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
    folder TEXT NOT NULL DEFAULT '', duration_s REAL, sample_rate INTEGER,
    channels INTEGER, added_at TEXT NOT NULL, last_scanned_at TEXT NOT NULL,
    file_size INTEGER NOT NULL, file_mtime REAL NOT NULL, file_hash TEXT,
    segment_candidates_found INTEGER, segments_capped INTEGER,
    effective_sensitivity REAL
);
CREATE TABLE analysis (
    sample_id INTEGER NOT NULL UNIQUE REFERENCES samples(id) ON DELETE CASCADE,
    tempo_bpm REAL, onset_count INTEGER
);
CREATE TABLE classification (
    sample_id INTEGER NOT NULL UNIQUE REFERENCES samples(id) ON DELETE CASCADE,
    facet_a TEXT, facet_b TEXT, provenance TEXT NOT NULL DEFAULT 'automatic',
    source_model TEXT, is_user_confirmed INTEGER NOT NULL DEFAULT 0
);
"""


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_v2_index_is_migrated_in_place(tmp_path):
    db_path = tmp_path / "old.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(_V2_SHAPE + "PRAGMA user_version = 2;")
    raw.execute(
        "INSERT INTO samples (filepath, filename, added_at, last_scanned_at, "
        "file_size, file_mtime) VALUES ('C:/x.wav', 'x.wav', "
        "'2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00', 1, 1.0)"
    )
    raw.execute("INSERT INTO classification (sample_id, facet_b) VALUES (1, 'loop')")
    raw.commit()
    raw.close()

    conn = open_db(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "content_changed_at" in _columns(conn, "samples")
        assert "analyzed_at" in _columns(conn, "analysis")
        cls = _columns(conn, "classification")
        assert {"content_class", "structural_type", "confidence"} <= cls
        assert not {"facet_a", "facet_b"} & cls
        # Data survives the rename; backfill = content as of first sight.
        assert conn.execute("SELECT structural_type FROM classification").fetchone()[0] == "loop"
        assert (
            conn.execute("SELECT content_changed_at FROM samples").fetchone()[0]
            == "2026-09-01T00:00:00+00:00"
        )
    finally:
        conn.close()
    open_db(db_path).close()  # re-opening an already-migrated DB is a no-op


def test_unversioned_phase1_index_is_migrated(tmp_path):
    # Build 58da501 left user_version at 0 with only `samples`: the Phase 2
    # tables must be created in their final shape and the ALTERs skip them.
    db_path = tmp_path / "phase1.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(_V2_SHAPE.split("CREATE TABLE analysis")[0])
    raw.close()

    conn = open_db(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "content_changed_at" in _columns(conn, "samples")
        assert "analyzed_at" in _columns(conn, "analysis")
        assert "structural_type" in _columns(conn, "classification")
    finally:
        conn.close()


def test_file_database_uses_wal_with_a_busy_timeout(tmp_path):
    # 2026-09-06 review: a GUI will read while a CLI run writes for hours.
    conn = open_db(tmp_path / "crate.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_old_index_gains_needs_review(tmp_path):
    db_path = tmp_path / "old.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(_V2_SHAPE + "PRAGMA user_version = 2;")
    raw.close()
    conn = open_db(db_path)  # v2 -> current in one open
    try:
        assert "needs_review" in _columns(conn, "segments")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


# --- Migration v8 (2026-09-08): segments.detection_method accepts 'window' ---


def test_v7_index_gains_the_window_kind_without_losing_a_row(tmp_path):
    """The allowed values are a CHECK constraint, so v8 rebuilds `segments`;
    every dependent row, the confirmed flags, the index and the cascade must
    come through — with foreign keys ON, the rebuild's DROP would cascade."""
    db_path = tmp_path / "v7.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        SCHEMA.replace("IN ('auto', 'manual', 'window')", "IN ('auto', 'manual')")
        + "PRAGMA user_version = 7;"
    )
    assert not segments_accept_windows(raw)
    raw.execute(
        "INSERT INTO samples (id, filepath, filename, added_at, last_scanned_at, file_size, file_mtime) "
        "VALUES (1, 'x.wav', 'x.wav', 't', 't', 1, 1.0)"
    )
    raw.execute(
        "INSERT INTO segments (id, sample_id, start_ms, end_ms, detection_method, strength) "
        "VALUES (1, 1, 0, 500, 'auto', 0.75)"
    )
    raw.execute(
        "INSERT INTO segments (id, sample_id, start_ms, end_ms, detection_method, is_user_confirmed, "
        "needs_review, cache_path) VALUES (2, 1, 100, 200, 'manual', 1, 1, 'c.wav')"
    )
    raw.execute("INSERT INTO segment_analysis (segment_id, rms_db) VALUES (1, -10.0)")
    raw.execute("INSERT INTO segment_embedding (segment_id, model_name, vector) VALUES (2, 'clap', x'0000')")
    raw.execute("INSERT INTO segment_classification (segment_id, content_class) VALUES (1, 'rhythmic')")
    raw.execute("INSERT INTO map_layout (id, computed_at, is_current) VALUES (1, 't', 1)")
    raw.execute("INSERT INTO map_position (layout_id, segment_id, map_x, map_y) VALUES (1, 1, 0.5, 0.5)")
    raw.commit()
    raw.close()

    conn = open_db(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 10
        assert _columns(conn, "libraries") >= {"path", "is_root", "in_scope"}       # v9 came along
        assert segments_accept_windows(conn)
        rows = conn.execute(
            "SELECT id, start_ms, end_ms, detection_method, is_user_confirmed, needs_review, strength, cache_path "
            "FROM segments ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            (1, 0, 500, "auto", 0, 0, 0.75, None),
            (2, 100, 200, "manual", 1, 1, None, "c.wav"),
        ]
        assert conn.execute("SELECT rms_db FROM segment_analysis WHERE segment_id = 1").fetchone()[0] == -10.0
        assert conn.execute("SELECT COUNT(*) FROM segment_embedding WHERE segment_id = 2").fetchone()[0] == 1
        assert conn.execute("SELECT content_class FROM segment_classification WHERE segment_id = 1").fetchone()[0] == "rhythmic"
        assert conn.execute("SELECT segment_id FROM map_position").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'idx_segments_sample'").fetchone()

        conn.execute("INSERT INTO segments (sample_id, start_ms, end_ms, detection_method) VALUES (1, 0, 10000, 'window')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO segments (sample_id, start_ms, end_ms, detection_method) VALUES (1, 0, 10000, 'bogus')")
        conn.execute("DELETE FROM samples WHERE id = 1")                     # the cascade still works
        assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM segment_analysis").fetchone()[0] == 0
        conn.rollback()
    finally:
        conn.close()
    open_db(db_path).close()                                                 # re-opening is a no-op


def test_ids_clause_restricts_to_these_and_nothing_else():
    """§9.6 anchored only / Caption this sample: None = no restriction, an
    empty list = nothing (never silently everything)."""
    assert ids_clause(None) == ("", [])
    assert ids_clause([]) == (" AND 0", [])
    assert ids_clause((3, "5")) == (" AND s.id IN (?,?)", [3, 5])
    assert ids_clause([7], column="g.sample_id") == (" AND g.sample_id IN (?)", [7])


# --- Migration v10 (2026-09-08): samples.id / segments.id never reused ---


def test_v9_index_gets_autoincrement_ids_and_keeps_every_row(tmp_path):
    """A plain INTEGER PRIMARY KEY reuses the largest deleted id; v10 rebuilds
    `samples` and `segments` with AUTOINCREMENT so a redone segment (or a
    folder scanned in after a removal) never takes an id something else
    held — the anchor and `map_position` keep ids across recomputes."""
    db_path = tmp_path / "v9.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(SCHEMA.replace(" AUTOINCREMENT", "") + "PRAGMA user_version = 9;")
    assert not ids_autoincrement(raw, "samples") and not ids_autoincrement(raw, "segments")
    raw.execute(
        "INSERT INTO samples (id, filepath, filename, added_at, last_scanned_at, file_size, file_mtime) "
        "VALUES (1, 'x.wav', 'x.wav', 't', 't', 1, 1.0)"
    )
    raw.execute("INSERT INTO analysis (sample_id, analyzed_at) VALUES (1, 't')")
    raw.execute(
        "INSERT INTO segments (id, sample_id, start_ms, end_ms, detection_method, strength) "
        "VALUES (1, 1, 0, 500, 'auto', 0.75)"
    )
    raw.execute(
        "INSERT INTO segments (id, sample_id, start_ms, end_ms, detection_method, is_user_confirmed) "
        "VALUES (2, 1, 100, 200, 'manual', 1)"
    )
    raw.execute("INSERT INTO segment_analysis (segment_id, rms_db) VALUES (2, -10.0)")
    raw.execute("INSERT INTO libraries (path, is_root, in_scope, added_at) VALUES ('C:\\lib', 1, 1, 't')")
    raw.commit()
    raw.close()

    conn = open_db(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 10
        assert ids_autoincrement(conn, "samples") and ids_autoincrement(conn, "segments")
        assert [tuple(r) for r in conn.execute("SELECT id, filepath FROM samples")] == [(1, "x.wav")]
        assert [tuple(r) for r in conn.execute(
            "SELECT id, sample_id, start_ms, end_ms, detection_method, is_user_confirmed FROM segments ORDER BY id"
        )] == [(1, 1, 0, 500, "auto", 0), (2, 1, 100, 200, "manual", 1)]
        assert conn.execute("SELECT rms_db FROM segment_analysis WHERE segment_id = 2").fetchone()[0] == -10.0
        assert conn.execute("SELECT analyzed_at FROM analysis WHERE sample_id = 1").fetchone()[0] == "t"
        assert conn.execute("SELECT COUNT(*) FROM libraries").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        # The point of it: a re-detected segment does not get the deleted one's id.
        conn.execute("DELETE FROM segments WHERE id = 2")
        new_id = conn.execute(
            "INSERT INTO segments (sample_id, start_ms, end_ms, detection_method) VALUES (1, 300, 400, 'auto')"
        ).lastrowid
        assert new_id == 3
        conn.execute("DELETE FROM samples WHERE id = 1")                    # cascades through segments
        assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 0
        assert conn.execute(
            "INSERT INTO samples (filepath, filename, added_at, last_scanned_at, file_size, file_mtime) "
            "VALUES ('y.wav', 'y.wav', 't', 't', 1, 1.0)"
        ).lastrowid == 2
        conn.commit()
        open_db(db_path).close()                                            # re-opening is a no-op
    finally:
        conn.close()
