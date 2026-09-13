"""Phase 12: analysis + segmentation as one pass per file (`describe.py`).

What is asserted: the fused pass writes exactly what the two stages in
sequence write — descriptors, types, segments and their descriptors — and
visits nothing twice; a window's descriptors read off the parent's frames
track the ones computed on the slice; a confirmed type drives the detection;
a stale parent's manual segments are refreshed; the worker path matches the
serial one; a stop ends the pass.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
import soundfile as sf

from crate.analysis import analyze_pending, describe_buffer, describe_window, frame_features, load_audio
from crate.corrections import set_structural_type
from crate.db import open_db
from crate.describe import describe_pending
from crate.scanner import scan_library
from crate.segmentation import create_manual_segment, segment_pending

SR = 22050


def _clicks(times_s, duration_s):
    y = np.zeros(int(SR * duration_s), dtype=np.float32)
    burst = int(0.02 * SR)
    for i, t in enumerate(times_s):
        s = int(t * SR)
        y[s : s + burst] += np.random.default_rng(i).normal(0, 0.5, burst).astype(np.float32)
    return np.clip(y, -1, 1)


def _tone(hz: float, duration_s: float = 1.0):
    t = np.arange(int(SR * duration_s)) / SR
    return (0.5 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def _library(root):
    root.mkdir()
    sf.write(root / "hit.wav", _clicks([0.0], 0.4), SR)
    sf.write(root / "loop.wav", _clicks([i * 0.5 for i in range(8)], 4.0), SR)
    sf.write(root / "tone.wav", _tone(330.0), SR)
    return root


@pytest.fixture()
def two_indexes(tmp_path):
    """The same small library scanned into two indexes — one for the two
    stages in sequence, one for the fused pass."""
    lib = _library(tmp_path / "lib")
    a, b = open_db(tmp_path / "a.db"), open_db(tmp_path / "b.db")
    scan_library(a, lib)
    scan_library(b, lib)
    yield a, b, lib
    a.close()
    b.close()


def _rows(conn, sql):
    return [tuple(r) for r in conn.execute(sql)]


def _id(conn, name):
    return conn.execute("SELECT id FROM samples WHERE filename = ?", (name,)).fetchone()[0]


def test_the_fused_pass_writes_what_the_two_stages_write(two_indexes):
    a, b, _ = two_indexes
    analyze_pending(a)
    segment_pending(a)
    analysis, segmentation = describe_pending(b)

    assert analysis.analyzed == 3 and analysis.failed == 0
    assert segmentation.samples_segmented + segmentation.one_shots_skipped == 3
    cols = (
        "peak_db, rms_db, crest_factor, attack_ms, decay_ms, f0_hz, pitch_confidence, "
        "mfcc_mean, mfcc_var, spectral_contrast, spectral_centroid, spectral_flatness, "
        "onset_count, harmonic_ratio, is_loop, tempo_bpm"
    )
    q = f"SELECT s.filename, {cols} FROM analysis x JOIN samples s ON s.id = x.sample_id ORDER BY s.filename"
    assert _rows(a, q) == _rows(b, q)
    q = ("SELECT s.filename, k.structural_type, k.provenance FROM classification k "
         "JOIN samples s ON s.id = k.sample_id ORDER BY s.filename")
    assert _rows(a, q) == _rows(b, q)
    q = ("SELECT s.filename, g.start_ms, g.end_ms, ROUND(g.strength, 6) FROM segments g "
         "JOIN samples s ON s.id = g.sample_id ORDER BY s.filename, g.start_ms")
    assert _rows(a, q) == _rows(b, q) and _rows(a, q), "the loop should have segments, identical in both"
    q = ("SELECT s.filename, g.start_ms, sa.peak_db, sa.rms_db, sa.mfcc_mean, sa.spectral_centroid, "
         "sa.onset_count, sa.harmonic_ratio FROM segment_analysis sa JOIN segments g ON g.id = sa.segment_id "
         "JOIN samples s ON s.id = g.sample_id ORDER BY s.filename, g.start_ms")
    assert _rows(a, q) == _rows(b, q)
    q = "SELECT filename, segment_candidates_found, segments_capped FROM samples ORDER BY filename"
    assert _rows(a, q) == _rows(b, q)

    again = describe_pending(b)                      # nothing new or stale: nothing visited
    assert again[0].analyzed == 0 and again[1].samples_segmented + again[1].one_shots_skipped == 0


def test_window_descriptors_are_the_parents_frames_however_they_are_reached(tmp_path):
    """One definition of a window's descriptors: the parent's frames inside
    it. The fused pass reads them off the parent's frame features; a manual
    save, without those at hand, computes frames over a grid-aligned excerpt
    with context — and lands on the same numbers (the spectral ones exactly,
    since a frame is local; the HPSS-derived ones near, since its filter
    reaches into the context)."""
    from crate.segmentation import _describe_window

    path = tmp_path / "loop.wav"
    sf.write(path, _clicks([i * 0.5 for i in range(8)], 4.0), SR)
    y, sr = load_audio(path)
    frames = frame_features(y, sr)
    for start_ms, end_ms in ((0, 400), (480, 950), (1000, 1490), (2210, 2470)):
        window = describe_window(frames, y, sr, start_ms, end_ms)
        excerpt = _describe_window(y, sr, start_ms, end_ms)
        assert window.peak_db == pytest.approx(excerpt.peak_db)
        assert window.rms_db == pytest.approx(excerpt.rms_db)
        # MFCCs carry an 80 dB floor relative to the loudest frame *in view*,
        # so the energy coefficient of a near-silent frame differs between
        # the parent and the excerpt; the shape coefficients are exact.
        a, b = np.asarray(json.loads(window.mfcc_mean)), np.asarray(json.loads(excerpt.mfcc_mean))
        assert np.allclose(a[1:], b[1:], rtol=1e-4, atol=1e-4)
        assert abs(a[0] - b[0]) < 0.05 * abs(b[0])
        assert window.spectral_centroid == pytest.approx(excerpt.spectral_centroid, rel=1e-6)
        assert window.spectral_flatness == pytest.approx(excerpt.spectral_flatness, rel=1e-6)
        assert abs(window.onset_count - excerpt.onset_count) <= 1
        assert window.harmonic_ratio == pytest.approx(excerpt.harmonic_ratio, abs=0.1)
        # and both see the same signal as a plain description of the slice does
        sliced, _ = describe_buffer(y[int(start_ms * sr / 1000):int(end_ms * sr / 1000)], sr)
        assert window.peak_db == pytest.approx(sliced.peak_db) and window.rms_db == pytest.approx(sliced.rms_db)
    empty = describe_window(frames, y, sr, 5000, 5100)     # past the end: silence, never an error
    assert empty.peak_db == empty.rms_db and empty.onset_count == 0


def test_a_confirmed_type_drives_the_detection_and_is_kept(two_indexes):
    a, _, _ = two_indexes
    describe_pending(a)
    loop = _id(a, "loop.wav")
    assert a.execute("SELECT COUNT(*) FROM segments WHERE sample_id = ?", (loop,)).fetchone()[0] > 0
    set_structural_type(a, [loop], "one-shot")                 # the user's call (Phase 11)

    analysis, segmentation = describe_pending(a, full=True)

    assert analysis.analyzed == 3 and analysis.skipped_confirmed == 1
    assert segmentation.one_shots_skipped >= 1                 # node S read the index, not the rule
    assert a.execute("SELECT COUNT(*) FROM segments WHERE sample_id = ?", (loop,)).fetchone()[0] == 0
    assert a.execute("SELECT structural_type FROM classification WHERE sample_id = ?", (loop,)).fetchone()[0] == "one-shot"


def test_a_stale_parent_refreshes_its_manual_segments(two_indexes):
    a, _, lib = two_indexes
    describe_pending(a)
    loop = _id(a, "loop.wav")
    manual = create_manual_segment(a, loop, 100, 600)
    a.execute("UPDATE segments SET cache_path = 'old.wav' WHERE id = ?", (manual,))
    a.commit()

    sf.write(lib / "loop.wav", _clicks([0.0], 0.3), SR)        # shorter now: the manual segment overruns
    scan_library(a, lib)
    analysis, segmentation = describe_pending(a)

    assert analysis.analyzed == 1 and analysis.refreshed_stale == 1
    assert segmentation.manual_flagged == 1 and segmentation.manual_kept == 1
    row = a.execute("SELECT needs_review, cache_path, detection_method FROM segments WHERE id = ?", (manual,)).fetchone()
    assert tuple(row) == (1, None, "manual")


def test_workers_match_serial_and_a_stop_ends_the_pass(two_indexes):
    a, b, _ = two_indexes
    serial = describe_pending(a)
    parallel = describe_pending(b, workers=2)
    assert (serial[0].analyzed, serial[1].segments_created) == (parallel[0].analyzed, parallel[1].segments_created)
    q = "SELECT s.filename, g.start_ms, g.end_ms FROM segments g JOIN samples s ON s.id = g.sample_id ORDER BY 1, 2"
    assert _rows(a, q) == _rows(b, q)

    polls: list[int] = []

    def stop() -> bool:
        polls.append(1)
        return len(polls) > 1                        # the first file goes through

    analysis, segmentation = describe_pending(a, full=True, should_stop=stop)
    assert analysis.stopped and segmentation.stopped
    assert analysis.analyzed == 1
