"""Tests for the list view's read-side queries (Phase 4.5)."""

from __future__ import annotations

import numpy as np
import soundfile as sf

from crate.analysis import analyze_pending
from crate.catalog import clock, hit_label, index_summary, load_samples, load_segments, load_windows
from crate.db import open_db
from crate.scanner import scan_library
from crate.segmentation import create_manual_segment, segment_pending

SR = 22050


def _clicks(times_s, duration_s):
    y = np.zeros(int(SR * duration_s))
    burst = int(0.02 * SR)
    for i, t in enumerate(times_s):
        s = int(t * SR)
        y[s : s + burst] += np.random.default_rng(i).normal(0, 0.5, burst)
    return np.clip(y, -1, 1).astype("float32")


def test_samples_carry_what_the_list_shows(tmp_path):
    lib = tmp_path / "lib"
    (lib / "Drums").mkdir(parents=True)
    sf.write(lib / "Drums" / "loop.wav", _clicks([i * 0.5 for i in range(8)], 4.0), SR)
    sf.write(lib / "hit.wav", _clicks([0.0], 0.4), SR)
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)
    segment_pending(conn)
    loop_id = conn.execute("SELECT id FROM samples WHERE filename = 'loop.wav'").fetchone()[0]
    conn.execute("INSERT INTO text_tags (sample_id, tag_or_caption, source_model, score) VALUES (?, 'kick drum', 'clap-zeroshot', 0.6), (?, 'snare drum', 'clap-zeroshot', 0.4), (?, 'rhythmic', 'clap-class', 0.9)", (loop_id, loop_id, loop_id))
    conn.commit()

    rows = load_samples(conn, top_tags=3)

    assert [r.filename for r in rows] == ["hit.wav", "loop.wav"]      # by folder, then name
    hit, loop = rows
    assert hit.folder == "" and loop.folder == "Drums"
    assert hit.structural_type == "one-shot" and loop.structural_type == "loop"
    assert loop.tempo_bpm is not None and hit.tempo_bpm is None
    assert loop.segment_count > 0 and hit.segment_count == 0
    assert loop.tags == "kick drum, snare drum"                        # best first, class rows excluded
    assert hit.tags == ""
    assert loop.clap_scores == {"rhythmic": 0.9} and hit.clap_scores == {}

    create_manual_segment(conn, loop_id, 100, 700)
    segments = load_segments(conn, loop_id)
    assert [s.start_ms for s in segments] == sorted(s.start_ms for s in segments)
    manual = [s for s in segments if s.detection_method == "manual"]
    assert len(manual) == 1 and manual[0].length_ms == 600

    summary = index_summary(conn)
    assert summary["samples"] == 2 and summary["analysed"] == 2 and summary["segments"] == len(segments)

    # A CLAP window of a long file (§6.4) is neither listed nor counted as a segment.
    conn.execute(
        "INSERT INTO segments (sample_id, start_ms, end_ms, detection_method) VALUES (?, 0, 4000, 'window')",
        (loop_id,),
    )
    conn.commit()
    assert load_segments(conn, loop_id) == segments
    assert [w.detection_method for w in load_windows(conn, loop_id)] == ["window"]
    assert [r.segment_count for r in load_samples(conn)] == [0, len(segments)]   # the manual one counts, the window not
    assert index_summary(conn)["segments"] == len(segments) and index_summary(conn)["windows"] == 1

    # The scope (§9.6): None = everything, () = nothing, folders = what lies under them.
    assert [r.filename for r in load_samples(conn, scope=None)] == ["hit.wav", "loop.wav"]
    assert load_samples(conn, scope=()) == []
    assert [r.filename for r in load_samples(conn, scope=[str(lib / "Drums")])] == ["loop.wav"]
    scoped = index_summary(conn, scope=[str(lib / "Drums")])
    assert scoped["samples"] == 1 and scoped["indexed"] == 2 and scoped["analysed"] == 1
    assert scoped["segments"] == len(segments) and scoped["windows"] == 1
    assert index_summary(conn, scope=())["samples"] == 0
    conn.close()


def test_hit_labels_read_as_positions():
    assert clock(1234) == "1.234 s" and clock(59_999) == "59.999 s"
    assert clock(60_000) == "1:00.000" and clock(430_250) == "7:10.250"
    assert hit_label(1234, 1484) == "hit @ 1.234 s (250 ms)"
    assert hit_label(430_250, 440_250, window=True) == "window @ 7:10.250 (10 s)"
