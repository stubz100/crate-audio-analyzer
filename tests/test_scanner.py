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

from crate.db import now_iso, open_db
from crate.scanner import _under_root_prefix, scan_library


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


# --- Regression tests for the 2026-09-06 review ---


def test_under_root_prefix_handles_drive_roots():
    # Finding 4: naive str(root)+os.sep doubled the separator on drive roots.
    assert _under_root_prefix(Path("D:\\")) == "D:\\"
    assert _under_root_prefix(Path("E:\\lib")) == "E:\\lib\\"
    # Paths strictly under the root match; siblings must not.
    assert "D:\\x.wav".startswith(_under_root_prefix(Path("D:\\")))
    assert not "E:\\library\\x.wav".startswith(_under_root_prefix(Path("E:\\lib")))


def test_identical_content_is_demoted_to_unchanged(scanned_tree):
    # Finding 3: a backup-restore/NAS-resync changes mtime but not bytes.
    root, conn = scanned_tree
    scan_library(conn, root)
    target = root / "a.wav"

    # First mtime change: no stored hash yet, so it reports changed and hashes.
    st = target.stat()
    os.utime(target, (st.st_atime, st.st_mtime + 10))
    second = scan_library(conn, root)
    assert second.changed == 1
    assert _rows(conn)[str(target)]["file_hash"] is not None

    # Second mtime change: stored hash matches -> demoted to unchanged.
    st = target.stat()
    os.utime(target, (st.st_atime, st.st_mtime + 10))
    third = scan_library(conn, root)
    assert third.changed == 0
    assert third.unchanged == 3  # a.wav (demoted) + b.wav + c.flac
    assert third.removed == 0


def test_unreadable_directory_protects_rows(scanned_tree, monkeypatch):
    # Finding 1: a locked/unreadable folder must not delete its rows.
    import crate.scanner as scanner_mod

    root, conn = scanned_tree
    scan_library(conn, root)
    before = set(_rows(conn))
    assert len(before) == 3

    real_scandir = scanner_mod.os.scandir
    locked = (root / "sub").resolve()

    def guarded_scandir(path, *args, **kwargs):
        if Path(path).resolve() == locked:
            raise PermissionError("simulated lock")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(scanner_mod.os, "scandir", guarded_scandir)
    summary = scan_library(conn, root)
    assert summary.walk_errors == 1
    assert summary.removed == 0  # rows under 'sub' survive the unreadable walk
    assert set(_rows(conn)) == before


def test_hash_failure_protects_row(scanned_tree, monkeypatch):
    # Finding 1: a file that cannot be hashed mid-scan keeps its row.
    import crate.scanner as scanner_mod

    root, conn = scanned_tree
    scan_library(conn, root)
    target = root / "a.wav"
    _write_audio(target, seconds=0.2)  # size change -> changed branch -> hash

    def boom(path):
        raise PermissionError("simulated lock")

    monkeypatch.setattr(scanner_mod, "_hash_file", boom)
    summary = scan_library(conn, root)
    assert summary.walk_errors == 1
    assert summary.removed == 0
    assert str(target) in _rows(conn)  # row survives, metadata untouched


def test_parent_root_rescan_refreshes_folder(tmp_path):
    # Finding 6: scanning a parent root must refresh `folder`, not leave it stale.
    root = tmp_path / "lib"
    _write_audio(root / "sub" / "x.wav")
    conn = open_db(tmp_path / "crate.db")
    try:
        scan_library(conn, root / "sub")
        row = _rows(conn)[str(root / "sub" / "x.wav")]
        assert row["folder"] == ""

        summary = scan_library(conn, root)
        assert summary.unchanged == 1
        assert _rows(conn)[str(root / "sub" / "x.wav")]["folder"] == "sub"
    finally:
        conn.close()


def test_file_becomes_unreadable_after_good_scan(scanned_tree):
    # Finding 14 gap: a file that corrupts after a good scan keeps its row.
    root, conn = scanned_tree
    scan_library(conn, root)
    target = root / "a.wav"
    target.write_bytes(b"corrupted")  # size + mtime change
    summary = scan_library(conn, root)
    assert summary.changed == 1
    assert summary.unreadable == 1
    row = _rows(conn)[str(target)]
    assert row["duration_s"] is None
    assert row["file_hash"] is not None


# --- Staleness flagging (2026-09-06 Phase 2 review, decision 4) ---


def test_content_changed_at_tracks_genuine_changes_only(scanned_tree):
    root, conn = scanned_tree
    scan_library(conn, root)
    target = root / "a.wav"
    first = _rows(conn)[str(target)]["content_changed_at"]
    assert first is not None

    scan_library(conn, root)  # unchanged re-scan: untouched
    assert _rows(conn)[str(target)]["content_changed_at"] == first

    _write_audio(target, seconds=0.2)  # genuine change: stamped
    scan_library(conn, root)
    second = _rows(conn)[str(target)]["content_changed_at"]
    assert second > first

    # Hash-identical retouch (mtime only, same bytes): content did not
    # change, so the stamp must not move — later phases must not re-analyze.
    st = target.stat()
    os.utime(target, (st.st_atime, st.st_mtime + 10))
    assert scan_library(conn, root).changed == 0
    assert _rows(conn)[str(target)]["content_changed_at"] == second


def test_scan_flags_stale_analysis_but_never_recomputes(scanned_tree):
    root, conn = scanned_tree
    scan_library(conn, root)
    sid = conn.execute("SELECT id FROM samples WHERE filename = 'a.wav'").fetchone()[0]
    conn.execute(
        "INSERT INTO analysis (sample_id, analyzed_at) VALUES (?, ?)", (sid, now_iso())
    )
    conn.commit()
    assert scan_library(conn, root).stale_analysis == 0

    _write_audio(root / "a.wav", seconds=0.2)
    summary = scan_library(conn, root)

    assert summary.stale_analysis == 1
    assert "stale analysis: 1" in summary.format()
    # Flagged, not recomputed (spec §9.6: nothing expensive runs by itself).
    assert conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 1
