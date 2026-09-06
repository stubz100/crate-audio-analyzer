"""Tests for the SQLite schema layer (spec §8)."""

from __future__ import annotations

import sqlite3

import pytest

from crate.db import SCHEMA, open_db


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
    assert version == 1


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