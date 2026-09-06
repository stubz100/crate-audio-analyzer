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
    is_segmentation_candidate,
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


def test_node_s_skips_clean_one_shots_only():
    assert is_segmentation_candidate(duration_s=0.3, onset_count=1) is False
    assert is_segmentation_candidate(duration_s=4.0, onset_count=8) is True
    # Long but single-onset: a one-shot only while the duration cap is on.
    assert is_segmentation_candidate(duration_s=6.0, onset_count=1) is True
    assert is_segmentation_candidate(
        duration_s=6.0, onset_count=1, one_shot_max_duration_s=None
    ) is False


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
    assert (0.01, 0.5100000000000001) not in bounds  # truncated, not left at 1.99 s
    assert bounds == [(0.01, 0.51), (2.0, 2.5)]
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

    assert summary.samples_segmented == 1          # node S skipped the one-shot
    assert summary.segments_created > 0
    assert summary.failed == 0
    segmented = conn.execute(
        "SELECT s.filename FROM samples s WHERE s.segments_detected_at IS NOT NULL"
    ).fetchall()
    assert [r[0] for r in segmented] == ["many.wav"]

    assert segment_pending(conn).samples_segmented == 0   # nothing new or stale
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
