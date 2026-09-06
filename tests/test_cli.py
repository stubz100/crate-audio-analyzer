"""Smoke tests for the console entry points (2026-09-06 review: a renamed
`main` once left `python -m crate.cli` raising NameError with no test to
notice; each command is run end to end here on a tiny library)."""

from __future__ import annotations

import numpy as np
import soundfile as sf

from crate.cli import analyze_main, scan_main, segment_main
from crate.db import open_db

SR = 22050


def _write(path, y: np.ndarray) -> None:
    sf.write(path, y.astype("float32"), SR, subtype="PCM_16")


def _clicks(times_s, duration_s) -> np.ndarray:
    y = np.zeros(int(SR * duration_s))
    burst = int(0.02 * SR)
    for i, t in enumerate(times_s):
        start = int(t * SR)
        y[start : start + burst] += np.random.default_rng(i).normal(0, 0.5, burst)
    return np.clip(y, -1.0, 1.0)


def test_scan_analyze_segment_end_to_end(tmp_path, capsys):
    lib = tmp_path / "lib"
    lib.mkdir()
    _write(lib / "hit.wav", _clicks([0.0], 0.4))
    _write(lib / "loop.wav", _clicks([i * 0.5 for i in range(8)], 4.0))
    db = tmp_path / "index.db"

    assert scan_main(["--root", str(lib), "--db", str(db)]) == 0
    assert "added 2" in capsys.readouterr().out

    assert analyze_main(["--db", str(db), "--one-shot-max-duration", "3"]) == 0
    assert "analyzed 2" in capsys.readouterr().out

    assert segment_main(["--db", str(db), "--max-segments", "3", "--sensitivity", "0.2"]) == 0
    out = capsys.readouterr().out
    assert "segmented 1 samples" in out and "one-shots skipped 1" in out

    conn = open_db(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 2
        assert 0 < conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] <= 3
    finally:
        conn.close()


def test_segment_rejects_nonsense_settings(tmp_path):
    db = tmp_path / "index.db"
    try:
        segment_main(["--db", str(db), "--sensitivity", "7"])
    except SystemExit as exc:
        assert exc.code == 2  # argparse usage error, not a traceback
    else:  # pragma: no cover
        raise AssertionError("a sensitivity of 7 must be refused")
