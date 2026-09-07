"""Tests for the process pool behind analysis and segmentation
(`parallel.py`, 2026-09-07): the same index comes out of two workers as of
one process, a stop keeps what finished, and the bounded map itself."""

from __future__ import annotations

import numpy as np
import soundfile as sf

from crate.analysis import analyze_pending
from crate.db import open_db
from crate.parallel import BoundedMap, make_pool
from crate.scanner import scan_library
from crate.segmentation import segment_pending

SR = 22050


def _clicks(times_s, duration_s):
    y = np.zeros(int(SR * duration_s))
    burst = int(0.02 * SR)
    for i, t in enumerate(times_s):
        s = int(t * SR)
        y[s : s + burst] += np.random.default_rng(i).normal(0, 0.5, burst)
    return np.clip(y, -1, 1).astype("float32")


def _library(tmp_path, loops: int = 3):
    lib = tmp_path / "lib"
    lib.mkdir()
    for i in range(loops):
        sf.write(lib / f"loop{i}.wav", _clicks([k * 0.5 for k in range(8)], 4.0 + 0.5 * i), SR)
    sf.write(lib / "hit.wav", _clicks([0.0], 0.4), SR)
    return lib


def _rows(conn, sql: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(sql)]


def test_workers_give_the_same_index_as_one_process(tmp_path):
    lib = _library(tmp_path)
    serial = open_db(tmp_path / "serial.db")
    parallel = open_db(tmp_path / "parallel.db")
    scan_library(serial, lib)
    scan_library(parallel, lib)

    a1 = analyze_pending(serial)
    a2 = analyze_pending(parallel, workers=2)
    assert (a1.analyzed, a1.failed) == (a2.analyzed, a2.failed) == (4, 0)
    q = (
        "SELECT s.filename, k.structural_type, round(a.rms_db, 3), a.onset_count "
        "FROM analysis a JOIN samples s ON s.id = a.sample_id "
        "JOIN classification k ON k.sample_id = s.id ORDER BY s.filename"
    )
    assert _rows(serial, q) == _rows(parallel, q)

    s1 = segment_pending(serial)
    s2 = segment_pending(parallel, workers=2)
    assert (s1.samples_segmented, s1.segments_created, s1.one_shots_skipped) == (
        s2.samples_segmented, s2.segments_created, s2.one_shots_skipped,
    )
    assert s1.segments_created > 0 and s1.one_shots_skipped == 1
    q = (
        "SELECT s.filename, g.start_ms, g.end_ms, round(g.strength, 3) FROM segments g "
        "JOIN samples s ON s.id = g.sample_id ORDER BY s.filename, g.start_ms"
    )
    assert _rows(serial, q) == _rows(parallel, q)
    q = "SELECT COUNT(*) FROM segment_analysis"
    assert _rows(serial, q) == _rows(parallel, q)
    serial.close()
    parallel.close()


def test_stop_with_workers_keeps_what_finished(tmp_path):
    lib = _library(tmp_path, loops=4)
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    polls: list[int] = []

    def stop() -> bool:
        polls.append(1)
        return len(polls) > 1                      # the first submission goes through

    summary = analyze_pending(conn, workers=2, should_stop=stop)

    assert summary.stopped and 0 < summary.analyzed < 5
    assert conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == summary.analyzed
    conn.close()


def _square(x: int) -> int:
    return x * x


def _boom(x: int) -> int:
    raise ValueError(f"no {x}")


def test_bounded_map_yields_results_errors_and_stops():
    with make_pool(2) as pool:
        squares = sorted(r for _, r, e in BoundedMap(pool, range(10), _square, lambda i: (i,)) if e is None)
        assert squares == [i * i for i in range(10)]

        errors = [e for _, _, e in BoundedMap(pool, [1, 2], _boom, lambda i: (i,))]
        assert len(errors) == 2 and all(isinstance(e, ValueError) for e in errors)

        polls: list[int] = []

        def stop() -> bool:
            polls.append(1)
            return len(polls) > 3

        mapping = BoundedMap(pool, range(100), _square, lambda i: (i,), stop, in_flight=2)
        results = list(mapping)
        assert mapping.stopped and 0 < len(results) < 100
        assert all(e is None for _, _, e in results)
