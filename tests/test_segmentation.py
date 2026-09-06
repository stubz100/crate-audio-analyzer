"""Tests for transient segmentation — nodes S and T (spec §6, Phase 3)."""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from crate.analysis import analyze_pending
from crate.db import open_db
from crate.scanner import scan_library
from crate.segmentation import (
    BOUNDARY_FIXED,
    BOUNDARY_TRANSIENT,
    PROFILE_LOOSE,
    PROFILE_TIGHT,
    SegmentationSettings,
    build_segments,
    choose_profile,
    create_manual_segment,
    delete_segment,
    detect,
    detect_transients,
    segment_pending,
    segment_sample,
    segments_for,
    update_segment,
)

SR = 22050


def _clicks(times_s, amps=None, duration_s=4.0) -> np.ndarray:
    """Percussive clicks at the given times, optionally with per-hit amplitude."""
    amps = amps or [1.0] * len(times_s)
    y = np.zeros(int(SR * duration_s), dtype=np.float64)
    burst = int(0.02 * SR)
    for i, (t, amp) in enumerate(zip(times_s, amps)):
        start = int(t * SR)
        noise = np.random.default_rng(i).normal(0, 0.5, burst) * np.linspace(1.0, 0.0, burst)
        y[start : start + burst] += amp * noise
    return np.clip(y, -1.0, 1.0)


def _write(path, y, sr: int = SR) -> None:
    sf.write(path, y.astype("float32"), sr, subtype="PCM_16")


@pytest.fixture()
def indexed(tmp_path):
    """A library with one 8-hit loop-ish file, scanned and analyzed."""
    lib = tmp_path / "lib"
    lib.mkdir()
    _write(lib / "eight_hits.wav", _clicks([i * 0.5 for i in range(8)], duration_s=4.0))
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)
    yield lib, conn
    conn.close()


# --- settings -----------------------------------------------------------------


def test_settings_reject_nonsense():
    for kwargs in (
        {"profile": "sharp"},
        {"boundary_mode": "until-it-stops"},
        {"min_length_unit": "ms"},
        {"sensitivity": 1.5},
        {"max_segments": 0},
        {"min_length": 0},
    ):
        with pytest.raises(ValueError):
            SegmentationSettings(**kwargs)


def test_length_rules_accept_seconds_or_percent_of_parent():
    """Spec §6.2: min/max length is 'seconds or % of parent duration'."""
    seconds = SegmentationSettings(min_length=0.1, max_length=1.0)
    assert seconds.min_length_s(10.0) == pytest.approx(0.1)
    assert seconds.max_length_s(10.0) == pytest.approx(1.0)

    percent = SegmentationSettings(
        min_length=5, min_length_unit="%", max_length=25, max_length_unit="%"
    )
    assert percent.min_length_s(8.0) == pytest.approx(0.4)
    assert percent.max_length_s(8.0) == pytest.approx(2.0)


# --- node S: the gate ----------------------------------------------------------


def test_segment_descriptor_fields_cover_every_segment_analysis_column():
    """The segment table is filled from CoreDescriptors by construction; keep
    the two in lockstep the same way test_analysis does for `analysis`."""
    from crate.segmentation import _SEGMENT_DESCRIPTOR_FIELDS

    conn = open_db(":memory:")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(segment_analysis)")}
    conn.close()

    assert set(_SEGMENT_DESCRIPTOR_FIELDS) | {"segment_id", "analyzed_at", "tempo_bpm"} == cols


def test_auto_profile_follows_structural_type():
    auto = SegmentationSettings()
    assert choose_profile(auto, "loop") == PROFILE_TIGHT
    assert choose_profile(auto, "multi-hit") == PROFILE_LOOSE
    forced = SegmentationSettings(profile=PROFILE_TIGHT)
    assert choose_profile(forced, "multi-hit") == PROFILE_TIGHT


# --- node T: detection ----------------------------------------------------------


@pytest.mark.parametrize("profile", [PROFILE_TIGHT, PROFILE_LOOSE])
def test_both_profiles_find_the_hits(profile):
    """Spec §12 Phase 3 deliverable: 'detection (both profiles)'."""
    y = _clicks([i * 0.5 for i in range(8)], duration_s=4.0)
    transients = detect_transients(y, SR, SegmentationSettings(), profile)

    assert len(transients) == 8
    for expected, (actual, strength) in zip([i * 0.5 for i in range(8)], transients):
        assert actual == pytest.approx(expected, abs=0.06)
        assert 0.0 <= strength <= 1.0


def test_sensitivity_filters_weak_transients():
    y = _clicks([0.0, 1.0, 2.0, 3.0], amps=[1.0, 0.05, 1.0, 0.05], duration_s=4.0)

    permissive = detect_transients(y, SR, SegmentationSettings(sensitivity=0.01), PROFILE_TIGHT)
    strict = detect_transients(y, SR, SegmentationSettings(sensitivity=0.6), PROFILE_TIGHT)

    assert len(permissive) > len(strict)
    assert len(strict) == 2  # only the two loud hits survive


def test_transient_to_transient_boundaries_chain():
    transients = [(0.0, 1.0), (1.0, 0.9), (2.0, 0.8)]
    result = build_segments(transients, 3.0, SegmentationSettings(max_length=5.0))

    bounds = [(s.start_s, s.end_s) for s in result.segments]
    assert bounds == [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]  # last runs to the end


def test_fixed_length_mode_ignores_the_next_onset():
    transients = [(0.0, 1.0), (0.2, 0.9)]
    settings = SegmentationSettings(boundary_mode=BOUNDARY_FIXED, max_length=0.5)

    result = build_segments(transients, 4.0, settings)

    # Each segment runs the full fixed length, overlapping the next onset.
    assert [(s.start_s, s.end_s) for s in result.segments] == [(0.0, 0.5), (0.2, 0.7)]


def test_too_short_is_dropped_and_too_long_is_truncated():
    """Spec §6.2: short → dropped (noise); long → truncated, NOT dropped."""
    transients = [(0.0, 1.0), (0.01, 0.9), (2.0, 0.8)]
    settings = SegmentationSettings(
        min_length=0.05, max_length=0.5, boundary_mode=BOUNDARY_TRANSIENT
    )

    result = build_segments(transients, 5.0, settings)

    bounds = [(s.start_s, s.end_s) for s in result.segments]
    assert (0.0, 0.01) not in bounds                 # 10 ms gap dropped
    assert bounds == [(0.01, 0.51), (2.0, 2.5)]     # 0.01 s hit truncated at 0.5 s, not left at 1.99 s
    assert all(s.end_s - s.start_s <= 0.5 + 1e-9 for s in result.segments)


def test_cap_keeps_the_strongest_and_flags_the_sample():
    transients = [(0.0, 0.2), (1.0, 1.0), (2.0, 0.4), (3.0, 0.9), (4.0, 0.3)]
    settings = SegmentationSettings(max_segments=2, max_length=5.0)

    result = build_segments(transients, 5.0, settings)

    assert result.candidates_found == 5
    assert result.capped is True
    assert [s.start_s for s in result.segments] == [1.0, 3.0]  # strongest two, in time order
    assert result.effective_sensitivity == pytest.approx(0.9)


def test_cap_that_does_not_bind_leaves_no_warning():
    transients = [(0.0, 1.0), (1.0, 0.5)]
    result = build_segments(transients, 2.0, SegmentationSettings(max_segments=5, max_length=5.0))

    assert result.capped is False
    assert result.effective_sensitivity is None
    assert result.candidates_found == 2


def test_detect_end_to_end_produces_sane_windows():
    y = _clicks([i * 0.5 for i in range(8)], duration_s=4.0)
    result = detect(y, SR, SegmentationSettings(max_length=5.0), "loop")

    assert result.profile == PROFILE_TIGHT
    assert len(result.segments) == 5  # default cap
    for s in result.segments:
        assert 0.0 <= s.start_s < s.end_s <= 4.0


# --- persistence -------------------------------------------------------------------


def test_segment_sample_writes_rows_counters_and_descriptors(indexed):
    lib, conn = indexed
    sample_id, filepath = conn.execute("SELECT id, filepath FROM samples").fetchone()[:2]

    result = segment_sample(conn, sample_id, filepath, "loop", SegmentationSettings())

    assert result is not None and result.segments
    rows = segments_for(conn, sample_id)
    assert len(rows) == len(result.segments)
    assert {r["detection_method"] for r in rows} == {"auto"}
    assert all(r["end_ms"] > r["start_ms"] for r in rows)
    assert all(r["detected_at"] for r in rows)

    parent = conn.execute(
        "SELECT segment_candidates_found, segments_capped, effective_sensitivity, "
        "segments_detected_at FROM samples WHERE id = ?", (sample_id,)
    ).fetchone()
    assert parent["segment_candidates_found"] == result.candidates_found
    assert parent["segments_capped"] == int(result.capped)
    assert parent["segments_detected_at"] is not None

    # Node C2's descriptor half: one row per segment, vectors intact.
    analysed = conn.execute(
        "SELECT COUNT(*) FROM segment_analysis sa JOIN segments s ON s.id = sa.segment_id "
        "WHERE s.sample_id = ? AND sa.mfcc_mean IS NOT NULL"
    , (sample_id,)).fetchone()[0]
    assert analysed == len(rows)
    # A segment is a one-shot by construction, so no tempo is stored (§6.4).
    assert conn.execute("SELECT COUNT(*) FROM segment_analysis WHERE tempo_bpm IS NOT NULL").fetchone()[0] == 0


def test_resegmentation_replaces_auto_but_keeps_manual(indexed):
    lib, conn = indexed
    sample_id, filepath = conn.execute("SELECT id, filepath FROM samples").fetchone()[:2]
    segment_sample(conn, sample_id, filepath, "loop", SegmentationSettings())
    auto_before = {(r["start_ms"], r["end_ms"]) for r in segments_for(conn, sample_id)}

    manual_id = create_manual_segment(conn, sample_id, 3100, 3400)
    segment_sample(conn, sample_id, filepath, "loop", SegmentationSettings())

    rows = segments_for(conn, sample_id)
    manual = [r for r in rows if r["detection_method"] == "manual"]
    assert len(manual) == 1 and manual[0]["id"] == manual_id
    assert (manual[0]["start_ms"], manual[0]["end_ms"]) == (3100, 3400)
    auto_after = {(r["start_ms"], r["end_ms"]) for r in rows if r["detection_method"] == "auto"}
    assert auto_after == auto_before  # deterministic re-run, no duplicates


def test_manual_segments_are_exempt_from_every_constraint(indexed):
    """Spec §6.3: no min/max length check, no cap, no truncation."""
    lib, conn = indexed
    sample_id = conn.execute("SELECT id FROM samples").fetchone()[0]
    settings = SegmentationSettings(max_segments=2, max_length=0.2)

    tiny = create_manual_segment(conn, sample_id, 100, 110)      # 10 ms: under min_length
    huge = create_manual_segment(conn, sample_id, 0, 4000)       # 4 s: over max_length
    for i in range(3):                                           # more than the cap
        create_manual_segment(conn, sample_id, 500 + i, 600 + i)

    segment_sample(conn, sample_id, conn.execute(
        "SELECT filepath FROM samples WHERE id = ?", (sample_id,)).fetchone()[0],
        "multi-hit", settings)

    manual = [r for r in segments_for(conn, sample_id) if r["detection_method"] == "manual"]
    assert len(manual) == 5
    by_id = {r["id"]: r for r in manual}
    assert by_id[tiny]["end_ms"] - by_id[tiny]["start_ms"] == 10
    assert by_id[huge]["end_ms"] - by_id[huge]["start_ms"] == 4000


def test_manual_create_rejects_inverted_bounds(indexed):
    lib, conn = indexed
    sample_id = conn.execute("SELECT id FROM samples").fetchone()[0]

    with pytest.raises(ValueError):
        create_manual_segment(conn, sample_id, 500, 500)
    with pytest.raises(LookupError):
        create_manual_segment(conn, 9999, 0, 100)


def test_editing_a_segment_protects_it_and_drops_its_cache(indexed):
    lib, conn = indexed
    sample_id, filepath = conn.execute("SELECT id, filepath FROM samples").fetchone()[:2]
    segment_sample(conn, sample_id, filepath, "loop", SegmentationSettings())
    auto = segments_for(conn, sample_id)[0]
    conn.execute(
        "UPDATE segments SET cache_path = 'x.wav', cache_rendered_at = 'now' WHERE id = ?",
        (auto["id"],),
    )
    conn.commit()

    update_segment(conn, auto["id"], start_ms=auto["start_ms"] + 5)

    edited = conn.execute("SELECT * FROM segments WHERE id = ?", (auto["id"],)).fetchone()
    assert edited["detection_method"] == "manual"   # adjusting protects it (§6.3)
    assert edited["is_user_confirmed"] == 1
    assert edited["cache_path"] is None             # bounds moved, render is stale (§6.5)
    assert edited["cache_rendered_at"] is None

    # ... and it now survives re-detection.
    segment_sample(conn, sample_id, filepath, "loop", SegmentationSettings())
    assert conn.execute("SELECT COUNT(*) FROM segments WHERE id = ?", (auto["id"],)).fetchone()[0] == 1


def test_update_and_delete_reject_unknown_ids(indexed):
    lib, conn = indexed
    with pytest.raises(LookupError):
        update_segment(conn, 4242, start_ms=0)
    assert delete_segment(conn, 4242) is False


def test_delete_removes_a_manual_segment_and_its_analysis(indexed):
    lib, conn = indexed
    sample_id = conn.execute("SELECT id FROM samples").fetchone()[0]
    segment_id = create_manual_segment(conn, sample_id, 100, 900)
    assert conn.execute(
        "SELECT COUNT(*) FROM segment_analysis WHERE segment_id = ?", (segment_id,)
    ).fetchone()[0] == 1

    assert delete_segment(conn, segment_id) is True

    assert segments_for(conn, sample_id) == []
    assert conn.execute(
        "SELECT COUNT(*) FROM segment_analysis WHERE segment_id = ?", (segment_id,)
    ).fetchone()[0] == 0  # ON DELETE CASCADE


def test_deleting_a_sample_cascades_to_its_segments(indexed):
    lib, conn = indexed
    sample_id = conn.execute("SELECT id FROM samples").fetchone()[0]
    create_manual_segment(conn, sample_id, 100, 900)

    conn.execute("DELETE FROM samples WHERE id = ?", (sample_id,))
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM segment_analysis").fetchone()[0] == 0


# --- the driver ------------------------------------------------------------------------


def test_segment_pending_skips_one_shots_and_repeats_no_work(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    _write(lib / "one_shot.wav", _clicks([0.0], duration_s=0.4))
    _write(lib / "many.wav", _clicks([i * 0.5 for i in range(8)], duration_s=4.0))
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)

    summary = segment_pending(conn)

    assert summary.samples_segmented == 1          # node S skipped the one-shot ...
    assert summary.one_shots_skipped == 1          # ... which is stamped, not detected
    assert summary.segments_created > 0
    assert summary.failed == 0
    with_segments = conn.execute(
        "SELECT DISTINCT s.filename FROM samples s JOIN segments g ON g.sample_id = s.id"
    ).fetchall()
    assert [r[0] for r in with_segments] == ["many.wav"]

    again = segment_pending(conn)                  # nothing new or stale
    assert (again.samples_segmented, again.one_shots_skipped) == (0, 0)
    conn.close()


def test_segment_pending_redetects_stale_samples(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    target = lib / "many.wav"
    _write(target, _clicks([i * 0.5 for i in range(8)], duration_s=4.0))
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)
    segment_pending(conn)
    before = conn.execute("SELECT segments_detected_at FROM samples").fetchone()[0]

    _write(target, _clicks([0.0, 1.0, 2.0], duration_s=4.0))  # new content
    scan_library(conn, lib)
    analyze_pending(conn)

    summary = segment_pending(conn)

    assert summary.samples_segmented == 1
    assert conn.execute("SELECT segments_detected_at FROM samples").fetchone()[0] > before
    conn.close()


def test_resegment_flag_forces_every_candidate(indexed):
    lib, conn = indexed
    assert segment_pending(conn).samples_segmented == 1
    assert segment_pending(conn).samples_segmented == 0

    assert segment_pending(conn, resegment=True).samples_segmented == 1


def test_segment_pending_can_skip_the_descriptor_pass(indexed):
    lib, conn = indexed
    summary = segment_pending(conn, analyze_segments=False)

    assert summary.segments_created > 0
    assert conn.execute("SELECT COUNT(*) FROM segment_analysis").fetchone()[0] == 0


# --- 2026-09-06 application review ------------------------------------------------


def _loop_then_scan(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "x.wav"
    _write(f, _clicks([i * 0.5 for i in range(8)], duration_s=4.0))
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)
    segment_pending(conn)
    return lib, f, conn


def _replace_and_rescan(lib, f, conn, y):
    _write(f, y)
    scan_library(conn, lib)
    analyze_pending(conn)
    return segment_pending(conn)


def test_type_flip_clears_stale_auto_segments(tmp_path):
    """A loop replaced by a one-shot used to keep its old auto segments forever."""
    lib, f, conn = _loop_then_scan(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] > 0

    summary = _replace_and_rescan(lib, f, conn, _clicks([0.0], duration_s=0.4))

    assert conn.execute("SELECT structural_type FROM classification").fetchone()[0] == "one-shot"
    assert summary.one_shots_skipped == 1
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM samples WHERE segments_detected_at < content_changed_at"
    ).fetchone()[0] == 0                                  # no longer flagged stale
    assert segment_pending(conn).one_shots_skipped == 0   # and not revisited
    conn.close()


def test_manual_segment_is_redescribed_and_flagged_after_content_change(tmp_path):
    lib, f, conn = _loop_then_scan(tmp_path)
    sid = conn.execute("SELECT id FROM samples").fetchone()[0]
    mid = create_manual_segment(conn, sid, 3000, 3900)
    conn.execute("UPDATE segments SET cache_path = 'old.wav', cache_rendered_at = 'x' WHERE id = ?", (mid,))
    conn.commit()
    before = conn.execute("SELECT analyzed_at FROM segment_analysis WHERE segment_id = ?", (mid,)).fetchone()[0]

    # Shorter replacement: the manual segment now overruns the file.
    summary = _replace_and_rescan(lib, f, conn, _clicks([0.0, 1.0, 2.0], duration_s=3.2))

    row = conn.execute("SELECT * FROM segments WHERE id = ?", (mid,)).fetchone()
    assert (row["start_ms"], row["end_ms"]) == (3000, 3900)     # bounds never touched (§6.3)
    assert row["needs_review"] == 1                            # ... but flagged
    assert row["cache_path"] is None                           # render of the old audio dropped
    after = conn.execute("SELECT analyzed_at FROM segment_analysis WHERE segment_id = ?", (mid,)).fetchone()[0]
    assert after > before                                      # descriptors redone on the new audio
    assert summary.manual_flagged == 1 and summary.manual_kept == 1
    assert "needing review" in summary.format()
    conn.close()


def test_manual_segment_inside_the_new_file_is_unflagged(tmp_path):
    lib, f, conn = _loop_then_scan(tmp_path)
    sid = conn.execute("SELECT id FROM samples").fetchone()[0]
    mid = create_manual_segment(conn, sid, 100, 900)

    _replace_and_rescan(lib, f, conn, _clicks([0.0, 1.0, 2.0], duration_s=3.2))

    assert conn.execute("SELECT needs_review FROM segments WHERE id = ?", (mid,)).fetchone()[0] == 0
    conn.close()


def test_segments_created_counts_rows_not_candidates(indexed):
    lib, conn = indexed
    summary = segment_pending(conn)

    assert summary.segments_created == conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]


def test_one_exploding_file_does_not_abort_the_run(tmp_path, monkeypatch):
    import crate.segmentation as mod

    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("a.wav", "b_boom.wav", "c.wav"):
        _write(lib / name, _clicks([i * 0.5 for i in range(8)], duration_s=4.0))
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)
    real_load = mod.load_audio

    def boom_on_b(path):
        if "boom" in str(path):
            raise RuntimeError("simulated librosa edge case past decode")
        return real_load(path)

    monkeypatch.setattr(mod, "load_audio", boom_on_b)

    summary = segment_pending(conn)

    assert summary.samples_segmented == 2
    assert summary.failed == 1
    assert any("RuntimeError" in e for e in summary.error_samples)
    stamped = conn.execute("SELECT COUNT(*) FROM samples WHERE segments_detected_at IS NOT NULL").fetchone()[0]
    assert stamped == 2                                       # the failed one is rolled back, not stamped
    conn.close()
