"""Tests for the library scanner — pipeline node `A` (spec §7).

Uses small synthetic fixture trees (spec §3: all testing on a much smaller
subset), with real WAV/FLAC files written by soundfile.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from crate.db import open_db
from crate.scanner import scan_library


def _write_audio(path: Path, seconds: float = 0.05, samplerate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.zeros(int(seconds * samplerate), dtype=np.float32)
    sf.write(str(path), data, samplerate)
    return path


@pytest.fixture()
def scanned_tree(tmp_path):
    """A small pack: supported audio, an rx2, preset containers, nesting."""
    root = tmp_path / "lib"
    _write_audio(root / "a.wav")
    _write_audio(root / "sub" / "b.wav")
    _write_audio(root / "sub" / "c.flac")
    (root / "loop.rx2").write_bytes(b"REX2 pretend")
    (root / "preset.nki").write_bytes(b"kontakt pretend")
    (root / "readme.txt").write_text("not audio")
    db_path = tmp_path / "crate.db"
    conn = open_db(db_path)
    yield root, conn
    conn.close()


def _rows(conn):
    return {row["filepath"]: row for row in conn.execute("SELECT * FROM samples")}


def test_first_scan_populates_skeleton_rows(scanned_tree):
    root, conn = scanned_tree
    summary = scan_library(conn, root)

    assert (summary.added, summary.changed, summary.unchanged, summary.removed) == (3, 0, 0, 0)
    assert summary.skipped_rx2 == 1
    assert summary.skipped_other == {".nki": 1, ".txt": 1}

    rows = _rows(conn)
    assert len(rows) == 3
    row = rows[str(root / "a.wav")]
    assert row["filename"] == "a.wav"
    assert row["folder"] == ""  # file directly under the scan root
    assert row["sample_rate"] == 8000
    assert row["channels"] == 1
    assert row["duration_s"] == pytest.approx(0.05)
    assert row["file_hash"] is None  # hash only when size/mtime CHANGED (spec §8)
    sub_row = rows[str(root / "sub" / "b.wav")]
    assert sub_row["folder"] == "sub"


def test_extension_case_is_ignored(scanned_tree):
    root, conn = scanned_tree
    _write_audio(root / "LOUD.WAV")
    summary = scan_library(conn, root)
    assert summary.added == 4  # 3 from fixture + LOUD.WAV


def test_rescan_without_changes_touches_only(scanned_tree):
    root, conn = scanned_tree
    first = scan_library(conn, root)
    before = {fp: row["last_scanned_at"] for fp, row in _rows(conn).items()}

    second = scan_library(conn, root)
    assert (second.added, second.changed, second.removed) == (0, 0, 0)
    assert second.unchanged == 3
    after = {fp: row["last_scanned_at"] for fp, row in _rows(conn).items()}
    assert set(before) == set(after)
    assert all(after[fp] >= before[fp] for fp in before)


def test_new_and_removed_files(scanned_tree):
    root, conn = scanned_tree
    scan_library(conn, root)

    _write_audio(root / "sub" / "d.wav")
    os.remove(root / "a.wav")

    summary = scan_library(conn, root)
    assert summary.added == 1
    assert summary.removed == 1
    rows = _rows(conn)
    assert str(root / "a.wav") not in rows
    assert str(root / "sub" / "d.wav") in rows
    assert len(rows) == 3


def test_changed_file_is_rehashed(scanned_tree):
    root, conn = scanned_tree
    scan_library(conn, root)
    target = root / "sub" / "b.wav"
    assert _rows(conn)[str(target)]["file_hash"] is None

    _write_audio(target, seconds=0.2)  # different size + mtime
    summary = scan_library(conn, root)
    assert summary.changed == 1
    row = _rows(conn)[str(target)]
    assert row["file_hash"] is not None  # hash computed because size/mtime changed
    assert row["duration_s"] == pytest.approx(0.2)


def test_mtime_only_change_is_detected_and_hashed(scanned_tree):
    root, conn = scanned_tree
    scan_library(conn, root)
    target = root / "a.wav"
    st = target.stat()
    os.utime(target, (st.st_atime, st.st_mtime + 10))  # same size, newer mtime

    summary = scan_library(conn, root)
    assert summary.changed == 1
    assert _rows(conn)[str(target)]["file_hash"] is not None


def test_unreadable_header_keeps_row_with_null_metadata(scanned_tree):
    root, conn = scanned_tree
    (root / "corrupt.wav").write_bytes(b"this is not a wav file")

    summary = scan_library(conn, root)
    assert summary.added == 4
    assert summary.unreadable == 1
    row = _rows(conn)[str(root / "corrupt.wav")]
    assert row["duration_s"] is None
    assert row["sample_rate"] is None


def test_removal_is_scoped_to_the_scanned_root(tmp_path):
    db_path = tmp_path / "crate.db"
    conn = open_db(db_path)
    try:
        root_a, root_b = tmp_path / "libA", tmp_path / "libB"
        _write_audio(root_a / "one.wav")
        _write_audio(root_b / "two.wav")

        scan_library(conn, root_a)
        summary = scan_library(conn, root_b)  # different root, same DB

        rows = _rows(conn)
        assert len(rows) == 2  # root_a's row survived root_b's scan
        assert summary.removed == 0
    finally:
        conn.close()


def test_missing_root_raises(tmp_path):
    conn = open_db(tmp_path / "crate.db")
    try:
        with pytest.raises(NotADirectoryError):
            scan_library(conn, tmp_path / "does-not-exist")
    finally:
        conn.close()