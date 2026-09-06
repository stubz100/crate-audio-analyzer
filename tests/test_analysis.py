"""Tests for heuristic analysis — nodes B/C and Facet B typing (Phase 2)."""

from __future__ import annotations

import json

import numpy as np
import pytest
import soundfile as sf

from crate.analysis import (
    PITCH_GATE_HARMONIC_RATIO,
    Descriptors,
    _acid_tempo_bpm,
    _bar_aligned_tempo,
    _embedded_key,
    _envelope_times,
    _loop_decision,
    analyze_file,
    analyze_pending,
    structural_type,
)
from crate.db import open_db
from crate.scanner import scan_library

SR = 22050


def _write(path, y: np.ndarray, sr: int = SR) -> None:
    sf.write(path, y.astype("float32"), sr, subtype="PCM_16")


def _click(n_clicks: int, spacing_s: float, tail_s: float = 0.2) -> np.ndarray:
    """Percussive click train — decays fast, no sustained pitch."""
    total = int(SR * (n_clicks * spacing_s + tail_s))
    y = np.zeros(total, dtype=np.float64)
    for i in range(n_clicks):
        start = int(i * spacing_s * SR)
        burst = int(0.02 * SR)
        noise = np.random.default_rng(i).normal(0, 0.5, burst)
        y[start : start + burst] += noise * np.linspace(1.0, 0.0, burst)
    return np.clip(y, -1.0, 1.0)


def _tone(freq: float, seconds: float) -> np.ndarray:
    t = np.linspace(0, seconds, int(SR * seconds), endpoint=False)
    return 0.5 * np.sin(2 * np.pi * freq * t)


def _pluck(freq: float = 220.0, seconds: float = 1.0, tau: float = 0.15) -> np.ndarray:
    """Exponentially decaying tone: -20 dB point at tau * ln(10) after the peak."""
    t = np.arange(int(SR * seconds)) / SR
    return 0.8 * np.sin(2 * np.pi * freq * t) * np.exp(-t / tau)


def _swell(fade_in_s: float = 0.3, tau: float = 0.2, seconds: float = 1.5) -> np.ndarray:
    """Linear fade-in to a single peak, then exponential decay."""
    t = np.arange(int(SR * seconds)) / SR
    env = np.where(t < fade_in_s, t / fade_in_s, np.exp(-(t - fade_in_s) / tau))
    return 0.8 * np.sin(2 * np.pi * 330.0 * t) * env


# --- schema lockstep ---------------------------------------------------------


def test_descriptors_cover_every_analysis_column():
    """Descriptors must stay in lockstep with the analysis table (spec §8)."""
    conn = open_db(":memory:")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(analysis)")}
    conn.close()

    assert set(Descriptors.__dataclass_fields__) | {"sample_id"} == cols


# --- amplitude ----------------------------------------------------------------


def test_amplitude_descriptors_on_a_known_signal(tmp_path):
    wav = tmp_path / "tone.wav"
    _write(wav, _tone(440.0, 1.0))

    d = analyze_file(wav)

    assert d is not None
    # A 0.5-amplitude sine: peak -6 dB, RMS -9 dB, crest factor sqrt(2).
    assert d.peak_db == pytest.approx(-6.0, abs=0.5)
    assert d.rms_db == pytest.approx(-9.0, abs=0.5)
    assert d.crest_factor == pytest.approx(np.sqrt(2), abs=0.1)


def test_envelope_times_follow_the_envelope_not_the_waveform():
    """2026-09-06 review: the original measured on |y|, which hits zero every
    half-cycle, so every file reported a decay under 7 ms. A pluck with a
    known -20 dB point must report it."""
    pluck = _pluck(tau=0.15)  # -20 dB at 0.15 * ln(10) = 345 ms after the peak
    attack_ms, decay_ms = _envelope_times(pluck, SR, first_onset_s=None)

    assert attack_ms < 30.0            # starts at full amplitude
    assert 300.0 <= decay_ms <= 400.0


def test_envelope_times_measure_attack_to_the_peak():
    swell = _swell(fade_in_s=0.3, tau=0.2)  # 10%..100% over 270 ms; -20 dB at 460 ms
    attack_ms, decay_ms = _envelope_times(swell, SR, first_onset_s=None)

    assert 230.0 <= attack_ms <= 310.0
    assert 400.0 <= decay_ms <= 520.0


def test_envelope_times_via_analyze_file_are_sensible(tmp_path):
    wav = tmp_path / "pluck.wav"
    _write(wav, _pluck(tau=0.15))

    d = analyze_file(wav)

    assert d.attack_ms < 30.0
    assert 300.0 <= d.decay_ms <= 400.0


def test_sustained_sound_that_never_decays_reports_no_decay():
    _attack, decay_ms = _envelope_times(_tone(440.0, 1.0), SR, first_onset_s=None)

    assert decay_ms is None


# --- timbre / pitch ----------------------------------------------------------


def test_timbre_descriptors_are_vectors_not_scalars(tmp_path):
    """MFCC/contrast must keep their shape — a scalar mean would collapse the
    whole Timbre similarity axis (spec §5.1) into one number."""
    wav = tmp_path / "tone.wav"
    _write(wav, _tone(440.0, 0.5))

    d = analyze_file(wav)

    assert len(json.loads(d.mfcc_mean)) == 13
    assert len(json.loads(d.mfcc_var)) == 13
    assert len(json.loads(d.spectral_contrast)) == 7


def test_pitch_gate_runs_on_tonal_content(tmp_path):
    wav = tmp_path / "tone.wav"
    _write(wav, _tone(220.0, 1.0))

    d = analyze_file(wav)

    assert d.harmonic_ratio is not None
    assert d.harmonic_ratio >= PITCH_GATE_HARMONIC_RATIO
    assert d.f0_hz == pytest.approx(220.0, rel=0.1)
    assert 0.0 <= d.pitch_confidence <= 1.0


@pytest.mark.parametrize(
    "name, signal",
    [
        ("white_noise", np.random.default_rng(0).normal(0, 0.3, SR // 2)),
        ("click_train", _click(n_clicks=6, spacing_s=0.25)),
    ],
)
def test_pitch_gate_skips_percussive_content(tmp_path, name, signal):
    """§5.1: pitch is meaningless on noise/percussion, so yin must not run.

    Both populations sit well below the gate: HPSS puts white noise at ~0.50
    (its no-information midpoint) and clicks at ~0.00. The gate is 0.7 rather
    than 0.5 precisely so noise cannot land on the tonal side by chance.
    """
    wav = tmp_path / f"{name}.wav"
    _write(wav, signal)

    d = analyze_file(wav)

    assert d.harmonic_ratio is not None
    assert d.harmonic_ratio < PITCH_GATE_HARMONIC_RATIO
    assert d.f0_hz is None
    assert d.pitch_confidence is None


# --- onsets, loops, Facet B --------------------------------------------------


def test_bar_aligned_tempo_accepts_whole_beat_durations_only():
    assert _bar_aligned_tempo(4.0, 121.0) == pytest.approx(120.0)     # 8 beats
    assert _bar_aligned_tempo(2.7586, 174.0) == pytest.approx(174.0, abs=0.1)
    assert _bar_aligned_tempo(3.13, 143.6) is None                    # 7.49 beats
    assert _bar_aligned_tempo(1.0, 120.0) is None                     # 2 beats < a bar


def test_periodic_bar_aligned_click_train_reads_as_a_loop(tmp_path):
    wav = tmp_path / "loop.wav"
    _write(wav, _click(n_clicks=8, spacing_s=0.5, tail_s=0.0))  # 8 beats @ 120 = 4.0 s

    d = analyze_file(wav)

    assert d.onset_count >= 4
    assert d.is_loop == 1
    assert d.tempo_bpm == pytest.approx(120.0, rel=0.02)
    assert structural_type(d, duration_s=4.0) == "loop"


def test_single_short_hit_reads_as_one_shot_with_no_tempo(tmp_path):
    wav = tmp_path / "hit.wav"
    _write(wav, _click(n_clicks=1, spacing_s=0.0, tail_s=0.2))

    d = analyze_file(wav)

    assert d.is_loop == 0
    assert d.tempo_bpm is None  # 2026-09-06 decision: tempo only for loops
    assert structural_type(d, duration_s=0.22) == "one-shot"


def test_hit_with_quiet_echoes_is_still_one_shot(tmp_path):
    """Spec §4 says one *dominant* onset: faint repeats must not count."""
    lead_in = int(0.05 * SR)  # detectors need context before the first onset
    y = np.concatenate([np.zeros(lead_in), _click(n_clicks=1, spacing_s=0.0, tail_s=1.0)])
    for i in range(1, 4):  # three echoes at 8% of the hit
        start = lead_in + int(i * 0.25 * SR)
        burst = int(0.02 * SR)
        y[start : start + burst] += 0.08 * np.random.default_rng(10 + i).normal(0, 0.5, burst)
    wav = tmp_path / "echoes.wav"
    _write(wav, np.clip(y, -1, 1))

    d = analyze_file(wav)

    assert d.onset_count == 1
    assert structural_type(d, duration_s=1.0) == "one-shot"


def test_tonal_one_shot_gets_no_tempo(tmp_path):
    """A 0.4 s tone used to come back at 198.8 BPM (confidence 0.014)."""
    wav = tmp_path / "tone.wav"
    _write(wav, _tone(330.0, 0.4))

    d = analyze_file(wav)

    assert d.is_loop == 0
    assert d.tempo_bpm is None


def test_structural_type_multi_hit_for_aperiodic_bursts():
    d = Descriptors(onset_count=5, tempo_confidence=0.05, is_loop=0)

    assert structural_type(d, duration_s=3.0) == "multi-hit"


def test_acid_one_shot_flag_overrides_periodicity():
    """Embedded metadata is authoritative (spec §7 node C) — an ACID one-shot
    flag must win over a periodic-looking onset envelope."""
    d = Descriptors(onset_count=8, tempo_confidence=0.9)

    is_loop, tempo = _loop_decision(d, 4.0, {"acid": {"one_shot": True, "beats": 8}}, [120.0])

    assert (is_loop, tempo) == (False, None)


def test_acid_beat_count_alone_marks_a_loop_with_derived_tempo():
    """Real ACIDized loops often carry beats/meter with no flags set at all,
    and the stored tempo float is a nominal 120.0 — verified across 52 real
    files. 16 beats over 5.581 s is 172 BPM."""
    d = Descriptors(onset_count=1, tempo_confidence=0.0)
    acid = {"beats": 16, "tempo_bpm": 120.0, "one_shot": False, "acidized": False}

    is_loop, tempo = _loop_decision(d, 5.581, {"acid": acid}, [])

    assert is_loop is True
    assert tempo == pytest.approx(172.0, abs=0.1)
    assert _acid_tempo_bpm(acid, 5.581) == pytest.approx(172.0, abs=0.1)


def test_smpl_loop_points_do_not_mark_a_loop():
    """`smpl` loop points are a sampler's sustain region on an instrument note
    (spoken word, single vocal notes and synth chords carry them in this
    library), not tempo-synced material — reversed 2026-09-06."""
    d = Descriptors(onset_count=1, tempo_confidence=0.0)

    is_loop, tempo = _loop_decision(d, 0.3, {"smpl": {"loop_count": 1}}, [])

    assert (is_loop, tempo) == (False, None)


def test_key_comes_from_the_acid_root_note_only_when_set():
    assert _embedded_key({"acid": {"root_set": True, "root_note": 55}}) == "G"
    assert _embedded_key({"acid": {"root_set": True, "root_note": 61}}) == "C#"
    assert _embedded_key({"acid": {"root_set": False, "root_note": 60}}) is None
    assert _embedded_key({"smpl": {"midi_unity_note": 60}}) is None  # placeholder
    assert _embedded_key({}) is None


def test_undecodable_file_returns_none(tmp_path):
    bad = tmp_path / "broken.wav"
    bad.write_bytes(b"RIFF____WAVEnonsense")

    assert analyze_file(bad) is None


# --- analyze_pending -----------------------------------------------------------


def test_analyze_pending_populates_rows_and_skips_done_work(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    _write(lib / "tone.wav", _tone(330.0, 0.4))
    _write(lib / "hit.wav", _click(n_clicks=1, spacing_s=0.0, tail_s=0.15))

    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)

    first = analyze_pending(conn)
    assert first.analyzed == 2
    assert first.failed == 0
    assert first.refreshed_stale == 0

    rows = conn.execute(
        "SELECT COUNT(*) FROM analysis WHERE mfcc_mean IS NOT NULL AND analyzed_at IS NOT NULL"
    ).fetchone()[0]
    assert rows == 2
    facets = {r[0] for r in conn.execute("SELECT structural_type FROM classification")}
    assert facets <= {"one-shot", "multi-hit", "loop"}

    # Second run has nothing left to do — analysis is the expensive stage.
    assert analyze_pending(conn).analyzed == 0
    conn.close()


def test_changed_file_is_reanalyzed_on_the_next_explicit_run(tmp_path):
    """2026-09-06 decision 4: the scanner flags, crate-analyze refreshes."""
    lib = tmp_path / "lib"
    lib.mkdir()
    _write(lib / "a.wav", _tone(330.0, 0.4))
    _write(lib / "b.wav", _tone(440.0, 0.4))
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)
    stamps = dict(
        conn.execute(
            "SELECT s.filename, a.analyzed_at FROM analysis a JOIN samples s ON s.id = a.sample_id"
        ).fetchall()
    )

    _write(lib / "a.wav", _click(n_clicks=1, spacing_s=0.0, tail_s=0.3))  # new content
    scan = scan_library(conn, lib)
    assert scan.changed == 1
    assert scan.stale_analysis == 1

    summary = analyze_pending(conn)

    assert summary.analyzed == 1
    assert summary.refreshed_stale == 1
    after = dict(
        conn.execute(
            "SELECT s.filename, a.analyzed_at FROM analysis a JOIN samples s ON s.id = a.sample_id"
        ).fetchall()
    )
    assert after["a.wav"] > stamps["a.wav"]
    assert after["b.wav"] == stamps["b.wav"]  # untouched file not re-analyzed
    assert analyze_pending(conn).analyzed == 0
    conn.close()


def test_reanalyze_never_overwrites_a_confirmed_classification(tmp_path):
    """Spec §11 / CLAUDE.md: manual corrections survive re-analysis."""
    lib = tmp_path / "lib"
    lib.mkdir()
    _write(lib / "tone.wav", _tone(330.0, 0.4))

    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)

    conn.execute(
        "UPDATE classification SET structural_type = 'loop', provenance = 'manual', "
        "is_user_confirmed = 1"
    )
    conn.commit()

    summary = analyze_pending(conn, reanalyze=True)

    assert summary.skipped_confirmed == 1
    row = conn.execute(
        "SELECT structural_type, provenance, is_user_confirmed FROM classification"
    ).fetchone()
    assert tuple(row) == ("loop", "manual", 1)
    conn.close()


def test_limit_bounds_the_run(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    for i in range(3):
        _write(lib / f"t{i}.wav", _tone(220.0 + i * 40, 0.2))

    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)

    assert analyze_pending(conn, limit=2).analyzed == 2
    assert conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 2
    conn.close()


# --- one-shot duration cap setting (spec §9.6, 2026-09-06) ----------------------


def test_one_shot_duration_cap_is_a_setting():
    single = Descriptors(onset_count=1, is_loop=0)

    assert structural_type(single, duration_s=3.5) == "multi-hit"  # default cap 2 s
    assert structural_type(single, duration_s=3.5, one_shot_max_duration_s=5.0) == "one-shot"
    assert structural_type(single, duration_s=37.0, one_shot_max_duration_s=None) == "one-shot"
    # Off never promotes multi-onset material.
    assert structural_type(Descriptors(onset_count=3), 3.5, one_shot_max_duration_s=None) == "multi-hit"


def test_analyze_pending_honours_the_one_shot_cap(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    _write(lib / "long_tone.wav", _tone(220.0, 3.0))  # one onset at most, 3 s
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)

    analyze_pending(conn)
    assert conn.execute("SELECT structural_type FROM classification").fetchone()[0] == "multi-hit"

    analyze_pending(conn, reanalyze=True, one_shot_max_duration_s=None)
    assert conn.execute("SELECT structural_type FROM classification").fetchone()[0] == "one-shot"
    conn.close()
