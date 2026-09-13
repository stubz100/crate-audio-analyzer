"""Tests for lazy segment rendering (spec §6.5, Phase 4.5)."""

from __future__ import annotations

import os
import time

import numpy as np
import pytest
import soundfile as sf

from crate.analysis import analyze_pending
from crate.db import open_db
from crate.render import FADE_OUT_MS, cache_state, clear_cache, evict_cache, preview_source, render_segment
from crate.scanner import scan_library
from crate.segmentation import create_manual_segment, update_segment

SR = 44100


@pytest.fixture()
def parent(tmp_path):
    """A stereo 24-bit 2 s file with one indexed sample."""
    lib = tmp_path / "lib"
    lib.mkdir()
    t = np.arange(int(SR * 2.0)) / SR
    left = 0.8 * np.sin(2 * np.pi * 220 * t)
    right = 0.8 * np.sin(2 * np.pi * 330 * t)
    sf.write(lib / "tone.wav", np.stack([left, right], axis=1).astype("float32"), SR, subtype="PCM_24")
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)
    sid = conn.execute("SELECT id FROM samples").fetchone()[0]
    yield conn, sid, tmp_path / "cache"
    conn.close()


def test_render_slices_at_native_rate_keeps_channels_and_fades(parent):
    conn, sid, cache = parent
    seg = create_manual_segment(conn, sid, 500, 900)

    out = render_segment(conn, seg, cache)

    info = sf.info(out)
    assert out.exists() and out.parent == cache
    assert info.samplerate == SR and info.channels == 2 and info.subtype == "PCM_24"
    assert info.frames == int(0.4 * SR)
    audio, _ = sf.read(out, dtype="float32", always_2d=True)
    assert abs(audio[-1]).max() < 1e-3                      # faded out
    assert abs(audio[0]).max() < 1e-3                       # faded in
    fade = int(FADE_OUT_MS * SR / 1000)
    assert abs(audio[-fade - 100]).max() > 0.5              # ... only at the ends
    row = conn.execute("SELECT cache_path, cache_rendered_at FROM segments WHERE id = ?", (seg,)).fetchone()
    assert row[0] == str(out) and row[1] is not None


def _rendered_at(conn, seg):
    return conn.execute("SELECT cache_rendered_at FROM segments WHERE id = ?", (seg,)).fetchone()[0]


def test_render_is_cached_and_force_rerenders(parent):
    conn, sid, cache = parent
    seg = create_manual_segment(conn, sid, 100, 300)
    first = render_segment(conn, seg, cache)
    stamp, size = _rendered_at(conn, seg), first.stat().st_size

    assert render_segment(conn, seg, cache) == first
    # A hit is not rewritten — the render stamp and the bytes stand. (Its mtime
    # *is* touched: that is how eviction knows it was used, Phase 12.)
    assert _rendered_at(conn, seg) == stamp and first.stat().st_size == size

    import time
    time.sleep(0.01)
    render_segment(conn, seg, cache, force=True)
    assert _rendered_at(conn, seg) != stamp


# --- eviction (Phase 12, 2026-09-13) ---


def test_eviction_drops_the_least_recently_used_and_a_hit_counts_as_use(parent):
    conn, sid, cache = parent
    a, b, c = (create_manual_segment(conn, sid, s, s + 200) for s in (100, 400, 700))
    pa, pb, pc = (render_segment(conn, seg, cache) for seg in (a, b, c))
    now = time.time()
    for path, age in ((pa, 30), (pb, 20), (pc, 10)):
        os.utime(path, (now - age, now - age))
    render_segment(conn, a, cache)                          # a hit: a is the most recent now
    assert pa.stat().st_mtime > pb.stat().st_mtime

    one = pa.stat().st_size
    summary = evict_cache(conn, cache, max_bytes=2 * one + 1)
    assert (summary.removed, summary.orphans, summary.bytes_freed) == (1, 0, pb.stat().st_size if pb.exists() else one)
    assert pa.exists() and pc.exists() and not pb.exists()
    assert conn.execute("SELECT cache_path FROM segments WHERE id = ?", (b,)).fetchone()[0] is None
    assert conn.execute("SELECT cache_path FROM segments WHERE id = ?", (a,)).fetchone()[0] == str(pa)


def test_orphans_go_first_and_clear_empties_the_cache(parent):
    conn, sid, cache = parent
    a, b = (create_manual_segment(conn, sid, s, s + 200) for s in (100, 400))
    pa, pb = (render_segment(conn, seg, cache) for seg in (a, b))
    conn.execute("UPDATE segments SET cache_path = NULL WHERE id = ?", (a,))    # the index forgot it
    conn.commit()
    state = cache_state(conn, cache)
    assert (state.files, state.orphans) == (2, 1) and "1 no longer referenced" in state.format()

    summary = evict_cache(conn, cache, max_bytes=None)     # no limit: orphans only
    assert (summary.removed, summary.orphans) == (1, 1)
    assert not pa.exists() and pb.exists()

    summary = clear_cache(conn, cache)
    assert summary.removed == 1 and not pb.exists()
    assert cache_state(conn, cache).files == 0
    assert conn.execute("SELECT cache_path FROM segments WHERE id = ?", (b,)).fetchone()[0] is None


def test_a_render_under_a_limit_evicts_after_writing(parent):
    conn, sid, cache = parent
    a, b = (create_manual_segment(conn, sid, s, s + 200) for s in (100, 400))
    pa = render_segment(conn, a, cache, max_bytes=10_000_000)
    time.sleep(0.02)
    pb = render_segment(conn, b, cache, max_bytes=pa.stat().st_size + 1)   # room for one
    assert pb.exists() and not pa.exists()
    assert conn.execute("SELECT cache_path FROM segments WHERE id = ?", (a,)).fetchone()[0] is None
    assert cache_state(conn, cache).files == 1


def test_editing_a_segment_forces_a_fresh_render(parent):
    conn, sid, cache = parent
    seg = create_manual_segment(conn, sid, 100, 300)
    render_segment(conn, seg, cache)

    update_segment(conn, seg, end_ms=600)                  # clears cache_path (§6.5)
    out = render_segment(conn, seg, cache)

    assert sf.info(out).frames == int(0.5 * SR)


def test_segment_past_the_end_is_refused_not_rendered(parent):
    conn, sid, cache = parent
    seg = create_manual_segment(conn, sid, 2500, 2900)      # file is 2.0 s

    with pytest.raises(ValueError):
        render_segment(conn, seg, cache)
    assert conn.execute("SELECT cache_path FROM segments WHERE id = ?", (seg,)).fetchone()[0] is None


def test_overrunning_segment_is_clamped_to_the_file(parent):
    conn, sid, cache = parent
    seg = create_manual_segment(conn, sid, 1800, 2400)

    out = render_segment(conn, seg, cache)

    assert sf.info(out).frames == int(0.2 * SR)


def test_unknown_ids_raise(parent):
    conn, sid, cache = parent
    with pytest.raises(LookupError):
        render_segment(conn, 999, cache)
    with pytest.raises(LookupError):
        preview_source(conn, "sample", 999)
    with pytest.raises(ValueError):
        preview_source(conn, "crate", 1)


def test_preview_source_is_the_file_for_samples_and_the_render_for_segments(parent):
    conn, sid, cache = parent
    seg = create_manual_segment(conn, sid, 100, 300)

    assert preview_source(conn, "sample", sid).name == "tone.wav"
    assert preview_source(conn, "segment", seg, cache).name == f"seg_{seg}.wav"
