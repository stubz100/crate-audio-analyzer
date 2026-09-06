"""Heuristic analysis — pipeline nodes `B` and `C` (spec §7), Phase 2.

Node `B` decodes a file to a normalized mono buffer; node `C` derives the
amplitude / pitch / timbre / spectrum descriptor set (spec §5.1) plus tempo,
onset and loop-ness signals, and writes one `analysis` row. Facet B
(structural type, spec §4) is rule-based and lands in `classification`.

Rules this module exists to honor (the Facet B ones were tuned on 500
labeled library files — 220 from "loops" folders, 280 from "one shots"/"hits"
folders — on 2026-09-06; see the journal for the numbers):

* **The pitch gate is HPSS-derived, not pitch-derived** (§5.1). `harmonic_ratio`
  comes from harmonic/percussive source separation and decides whether pitch
  tracking runs at all; `yin` is the default f0 estimator, `pyin` is not used.
* **Onsets come from the percussive component, via superflux, and only
  dominant ones count** (§4 "one dominant onset"). Ringing partials of a glass
  or metal hit beat against each other and modulate the envelope, which a
  plain onset detector on the full mix reads as a burst of extra onsets — a
  foley one-shot pack came out 65% multi-hit for exactly this reason. HPSS
  strips the harmonic ring-out, superflux (`lag=2, max_size=3`) suppresses
  what tonal modulation remains, and an onset only counts if it is at least
  DOMINANT_ONSET_FRACTION of the strongest one.
* **Periodicity needs a genuine autocorrelation peak.** Taking the maximum of
  the autocorrelation inside the tempo window made every decaying texture
  "periodic" at 240 BPM (a decaying envelope's autocorrelation just falls
  with lag); 28 of 40 cinematic impacts were classified as loops. The
  envelope is detrended and only a local peak counts.
* **A loop is a whole number of beats long.** Tempo-syncable material is
  N beats long at its tempo; the ACIDized loops in the library satisfy this
  exactly (8/16/32 beats) and the false positives never did. The tolerance
  is expressed in beats, not percent — a percentage tolerance admits every
  long file.
* **Metadata outranks acoustics** (§7 node C): an ACID beat count is an
  exact tempo, and a "<n> BPM" token in the filename plus a whole-beat
  duration is nearly as reliable (140 of 140 such files in the labeled set
  were loops, none of the hits carried one). Acoustic tempo estimates are
  frequently a metrical multiple (half/double/triplet) of the true tempo,
  so an acoustically detected loop's `tempo_bpm` is "a tempo the file is a
  whole number of beats at", not necessarily the producer's.
* **Tempo is stored only for loops** (2026-09-06 decision): `beat_track`
  always returns *a* tempo, so a one-shot would otherwise carry a nonsense
  BPM that §9.5's tempo filter would match.
* **Manual corrections are never overwritten** (§11). A `classification` row
  with `is_user_confirmed = 1` is left alone by re-analysis.
* **Stale rows are refreshed on demand only.** The scanner flags a changed
  file (`samples.content_changed_at`); `analyze_pending` picks it up on the
  next explicit run — nothing recomputes by itself (§9.6).

Analysis decodes at ANALYSIS_SR mono: the descriptors are not sample-rate
specific and the library is ~110k files (§3), so resampling once is far
cheaper than analyzing at native rates. The `samples` row keeps the true
native `sample_rate`/`channels` from the header — this rate is analysis-only.

librosa is imported lazily inside the functions that need it — importing it
costs seconds, and neither the GUI nor `crate-scan` should pay that.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .db import now_iso, scope_clause
from .wavmeta import read_embedded_metadata

log = logging.getLogger(__name__)

ANALYSIS_SR = 22050          # mono analysis rate (librosa default)
N_MFCC = 13
_HOP = 512                   # spectral features / onset envelope: 23 ms at 22.05 kHz
_ENV_FRAME = 512             # amplitude envelope: RMS over 23 ms windows ...
_ENV_HOP = 64                # ... every 2.9 ms, so attack times resolve to ~3 ms
_SILENCE_FLOOR_DB = -120.0   # reported instead of -inf for digital silence
ENVELOPE_THRESHOLD = 0.1     # -20 dB relative to the peak (attack start / decay end)
_MIN_SPECTRAL_SAMPLES = 2048  # librosa's default n_fft; short segment windows are
                              # zero-padded up to it for the transform descriptors

# Pitch gate (§5.1): below this harmonic-energy share, pitch is meaningless on
# percussive/noise content, so yin is skipped entirely.
#
# Measured on this machine (librosa 1.0 HPSS), which is why the value is 0.7
# and not the intuitive 0.5:
#     sine / chord / noisy tone .... 0.955 - 0.998
#     white noise .................. 0.501 - 0.510   <- HPSS's "no information" midpoint
#     click train / single hit ..... 0.000
# 0.5 sits exactly ON the noise midpoint, so noise lands either side of it at
# random; 0.7 leaves a wide margin against both populations.
PITCH_GATE_HARMONIC_RATIO = 0.7
PITCH_FMIN_HZ = 40.0
PITCH_FMAX_HZ = 2000.0
_PITCH_STABLE_SEMITONES = 1.0  # +/- window counted as agreeing with the median

# Onsets (§4). An onset counts only if its superflux strength is at least this
# fraction of the strongest onset in the file, AND the amplitude envelope it
# starts is at least DOMINANT_ONSET_LOUDNESS of the file's loudest — onset
# strength lives in the dB domain, which flatters quiet echoes and rattles.
DOMINANT_ONSET_FRACTION = 0.3
DOMINANT_ONSET_LOUDNESS = 0.2
_SUPERFLUX = {"lag": 2, "max_size": 3}   # Böck & Widmer 2013, as in librosa's docs
_ONSET_LEAD_IN_FRAMES = 4                # silence prepended before onset detection: a
                                         # transient at sample 0 collides with the frame-
                                         # centering pad and is never picked otherwise
_LOUDNESS_WINDOW_S = 0.06                # +/- around an onset frame for its RMS peak (the
                                         # detected frame trails the transient by ~1-2 hops)

# Tempo / loop-ness.
TEMPO_MIN_BPM = 40.0
TEMPO_MAX_BPM = 240.0
LOOP_MIN_DURATION_S = 1.0
LOOP_MIN_ONSETS = 4                # acoustic tier only
LOOP_MIN_PERIODICITY = 0.30        # detrended-autocorrelation local-peak height
LOOP_MIN_BEATS = 4                 # at least one bar of 4/4
LOOP_BEAT_TOLERANCE = 0.15         # |beats - round(beats)| for an acoustic tempo estimate
LOOP_BEAT_TOLERANCE_NAMED = 0.10   # ... for a tempo taken from the filename
LOOP_MIN_COVERAGE = 0.5            # last dominant onset must lie past this share of the file
LOOP_MIN_GRID_FIT = 0.6            # share of dominant onsets on the 16th-note grid
_GRID_SUBDIV = 4
_GRID_TOLERANCE = 0.15             # of one grid step
_PERIODICITY_DETREND_S = 1.0       # moving-mean window removed before autocorrelation

# Facet B (spec §4). The one-shot duration cap is a user setting (spec §9.6,
# 2026-09-06): this is its default when on; None means duration-independent —
# any sample with at most one dominant onset is a one-shot, however long.
ONE_SHOT_MAX_DURATION_S: float | None = 2.0

_PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_FILENAME_BPM = re.compile(r"(?<!\d)(\d{2,3})\s*[-_ ]?\s*bpm", re.IGNORECASE)


@dataclass
class AnalysisSummary:
    analyzed: int = 0
    refreshed_stale: int = 0     # of `analyzed`: rows re-done because content changed
    failed: int = 0
    skipped_confirmed: int = 0   # classification protected by a manual correction
    stopped: bool = False        # stopped by request; what was done is kept
    elapsed_s: float = 0.0
    error_samples: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            f"analyzed {self.analyzed} (of which stale refreshed {self.refreshed_stale}) | "
            f"failed {self.failed} | protected classifications kept {self.skipped_confirmed}"
        ]
        lines.extend(f"  ! {s}" for s in self.error_samples)
        if self.stopped:
            lines.append("stopped by request; everything analyzed so far is kept")
        lines.append(f"elapsed: {self.elapsed_s:.1f}s")
        return "\n".join(lines)


def _db_scale(x: float) -> float:
    return _SILENCE_FLOOR_DB if x <= 0 else max(_SILENCE_FLOOR_DB, 20.0 * math.log10(x))


# --- amplitude envelope --------------------------------------------------------


def _rms_envelope(y: np.ndarray) -> np.ndarray:
    import librosa

    return librosa.feature.rms(y=y, frame_length=_ENV_FRAME, hop_length=_ENV_HOP)[0]


def _envelope_times(
    y: np.ndarray, sr: int, first_onset_s: float | None, env: np.ndarray | None = None
) -> tuple[float | None, float | None]:
    """Attack and decay in ms (spec §5.1), measured on a frame RMS envelope.

    The 2026-09-06 review found the original measured on the rectified
    waveform, which dips to zero every half-cycle — every file in a 322-file
    subset reported a decay under 7 ms. An RMS envelope (23 ms window, 2.9 ms
    hop) does not have that problem.

    * attack: from the last sub-threshold point before the peak to the peak
    * decay: from the peak to the first sub-threshold point after it (None if
      the sound never drops 20 dB below its peak before it ends)

    The peak is the first dominant onset's, not the file's global one — on a
    loop or a multi-hit take the global peak may be a later hit, and a
    sustained recording's "attack to the loudest moment" is meaningless (a
    kettle recording reported a 22 s attack).
    """
    if env is None:
        env = _rms_envelope(y)
    if env.size == 0 or not np.any(env > 0):
        return None, None
    frames_per_s = sr / _ENV_HOP
    if first_onset_s is None:
        peak_idx = int(np.argmax(env))
    else:
        # Search from just before the onset (the onset-strength peak sits on
        # the rising edge) for up to a second, or to the end.
        start = max(0, int((first_onset_s - 0.05) * frames_per_s))
        stop = min(env.size, int((first_onset_s + 1.0) * frames_per_s) + 1)
        window = env[start:stop]
        peak_idx = start + int(np.argmax(window)) if window.size else int(np.argmax(env))
    peak = float(env[peak_idx])
    if peak <= 0:
        return None, None
    threshold = peak * ENVELOPE_THRESHOLD

    before = np.flatnonzero(env[:peak_idx] < threshold)
    start_idx = int(before[-1]) + 1 if before.size else 0
    attack_ms = (peak_idx - start_idx) / frames_per_s * 1000.0

    after = np.flatnonzero(env[peak_idx:] <= threshold)
    decay_ms = float(after[0]) / frames_per_s * 1000.0 if after.size else None
    return float(attack_ms), decay_ms


# --- onsets and periodicity ----------------------------------------------------


def _dominant_onsets(
    onset_env: np.ndarray, sr: int, rms_env: np.ndarray, lead_in_frames: int = 0
) -> list[float]:
    """Times (s) of the onsets that count (spec §4 "dominant").

    `onset_env` may start with `lead_in_frames` of prepended silence (see
    _ONSET_LEAD_IN_FRAMES); returned times are relative to the real signal.
    Strength is relative to the strongest onset; loudness is the RMS-envelope
    peak within +/- _LOUDNESS_WINDOW_S of the onset, relative to the file's
    loudest frame.
    """
    import librosa

    frames = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=sr, hop_length=_HOP, units="frames"
    )
    if len(frames) == 0:
        return []
    strengths = onset_env[frames]
    strong = strengths >= DOMINANT_ONSET_FRACTION * float(strengths.max())
    loudest = float(rms_env.max()) if rms_env.size else 0.0
    env_per_hop = _HOP // _ENV_HOP
    half_window = int(_LOUDNESS_WINDOW_S * sr / _ENV_HOP)
    out: list[float] = []
    for frame, is_strong in zip(frames, strong):
        real_frame = int(frame) - lead_in_frames
        if not is_strong or real_frame < 0:
            continue
        if loudest > 0 and DOMINANT_ONSET_LOUDNESS > 0:
            centre = real_frame * env_per_hop
            i0 = max(0, centre - half_window)
            i1 = min(rms_env.size, centre + half_window + 1)
            amp = float(rms_env[i0:i1].max()) if i1 > i0 else 0.0
            if amp < DOMINANT_ONSET_LOUDNESS * loudest:
                continue
        out.append(real_frame * _HOP / sr)
    return out


def _periodicity(onset_env: np.ndarray, sr: int) -> tuple[float | None, float | None]:
    """(confidence, bpm) from the detrended onset envelope's autocorrelation.

    Confidence is the height of the strongest genuine LOCAL peak inside the
    tempo window — never the window's edge. Taking the window maximum read
    every decaying texture as periodic at TEMPO_MAX_BPM. The lag is refined
    by parabolic interpolation (frame-quantized lags limit tempo to ~3%).
    """
    import librosa
    from scipy.ndimage import uniform_filter1d
    from scipy.signal import find_peaks

    if onset_env.size < 8 or not np.any(onset_env):
        return None, None
    fps = sr / _HOP
    trend = uniform_filter1d(
        onset_env, size=max(3, int(round(fps * _PERIODICITY_DETREND_S))), mode="nearest"
    )
    x = onset_env - trend
    ac = librosa.autocorrelate(x, max_size=x.size)
    if ac.size < 4 or ac[0] <= 0:
        return None, None
    ac = ac / ac[0]
    min_lag = max(2, int(round(fps * 60.0 / TEMPO_MAX_BPM)))
    max_lag = min(ac.size - 2, int(round(fps * 60.0 / TEMPO_MIN_BPM)))
    if max_lag <= min_lag:
        return None, None
    segment = ac[min_lag : max_lag + 1]
    peaks, props = find_peaks(segment, height=0)
    if peaks.size == 0:
        return 0.0, None
    lag = int(peaks[np.argmax(props["peak_heights"])]) + min_lag
    a, b, c = float(ac[lag - 1]), float(ac[lag]), float(ac[lag + 1])
    denom = a - 2.0 * b + c
    delta = 0.5 * (a - c) / denom if denom != 0 else 0.0
    lag_f = lag + float(np.clip(delta, -0.5, 0.5))
    return float(np.clip(b, 0.0, 1.0)), 60.0 * fps / lag_f


def _grid_fit(onsets: list[float], bpm: float) -> float:
    """Share of onsets within _GRID_TOLERANCE of the best-phase 16th-note grid."""
    if len(onsets) < 2 or bpm <= 0:
        return 0.0
    step = 60.0 / bpm / _GRID_SUBDIV
    phases = (np.asarray(onsets) / step) % 1.0
    angles = phases * 2.0 * np.pi
    mean_phase = (
        np.arctan2(np.sin(angles).mean(), np.cos(angles).mean()) / (2.0 * np.pi)
    ) % 1.0
    distance = np.abs(((phases - mean_phase + 0.5) % 1.0) - 0.5)
    return float(np.mean(distance <= _GRID_TOLERANCE))


# --- pitch -----------------------------------------------------------------------


def _pitch(y: np.ndarray, sr: int) -> tuple[float | None, float | None]:
    """Median f0 over in-range frames, plus the fraction of frames agreeing.

    `yin` reports an estimate for every frame with no voicing decision, so
    "confidence" here is frame agreement: the share within one semitone of the
    median. `pyin` would give a true voicing probability but is the slowest
    descriptor by far (§5.1) and is reserved for an optional quality pass.
    """
    import librosa

    if y.size < sr // 10:
        return None, None
    f0 = librosa.yin(y, fmin=PITCH_FMIN_HZ, fmax=PITCH_FMAX_HZ, sr=sr, hop_length=_HOP)
    f0 = f0[np.isfinite(f0)]
    f0 = f0[(f0 >= PITCH_FMIN_HZ) & (f0 <= PITCH_FMAX_HZ)]
    if f0.size == 0:
        return None, None
    median = float(np.median(f0))
    ratio = _PITCH_STABLE_SEMITONES / 12.0
    low, high = median * 2.0**-ratio, median * 2.0**ratio
    agreeing = float(np.mean((f0 >= low) & (f0 <= high)))
    return median, agreeing


# --- node B + C ---------------------------------------------------------------------


@dataclass
class CoreDescriptors:
    """Everything derivable from a bare audio buffer, with no file context.

    `analysis` and `segment_analysis` are both filled from this — spec §8 makes
    the segment table a mirror of the sample one minus the file-level columns
    (`is_loop`, `key`, `embedded_metadata_json`) — so one function fills both
    and `Descriptors` *extends* this class rather than re-declaring its
    fields: a column added here reaches both tables with no name list to
    keep in step (2026-09-06 review).
    """

    onset_count: int = 0                   # dominant onsets on the percussive component
    tempo_confidence: float | None = None  # periodicity: detrended autocorrelation peak
    harmonic_ratio: float | None = None
    peak_db: float | None = None
    rms_db: float | None = None
    crest_factor: float | None = None
    attack_ms: float | None = None
    decay_ms: float | None = None
    f0_hz: float | None = None
    pitch_confidence: float | None = None
    mfcc_mean: str | None = None           # JSON array[13]
    mfcc_var: str | None = None            # JSON array[13]
    spectral_contrast: str | None = None   # JSON array[7]
    spectral_centroid: float | None = None
    spectral_bandwidth: float | None = None
    spectral_rolloff: float | None = None
    spectral_flatness: float | None = None


@dataclass
class Descriptors(CoreDescriptors):
    """One `analysis` row's worth of signals (spec §8): the buffer descriptors
    plus the file-level columns only a whole sample has."""

    analyzed_at: str | None = None         # set by _store
    tempo_bpm: float | None = None         # loops only (see module docstring)
    is_loop: int = 0
    key: str | None = None                 # pitch class from ACID root note, if set
    embedded_metadata_json: str | None = None


@dataclass
class LoopEvidence:
    """Working values the loop decision needs — never stored."""

    onsets: list[float] = field(default_factory=list)
    bpm_candidates: list[float] = field(default_factory=list)


def describe_buffer(
    y: np.ndarray, sr: int = ANALYSIS_SR
) -> tuple[CoreDescriptors, LoopEvidence]:
    """Node `C`'s descriptor work on one buffer — a whole file, or a segment
    window (node `C2`, spec §7). Never touches the filesystem."""
    import librosa

    c = CoreDescriptors()
    ev = LoopEvidence()
    if y.size == 0:
        c.peak_db = c.rms_db = _SILENCE_FLOOR_DB
        return c, ev

    # A segment window can be shorter than one FFT frame. Zero-pad a working
    # copy for every transform-based descriptor (HPSS included) so they are
    # well defined; the amplitude descriptors below still use the true buffer.
    # Padding at the tail leaves onset times unchanged.
    y_spec = (
        y
        if y.size >= _MIN_SPECTRAL_SAMPLES
        else np.pad(y, (0, _MIN_SPECTRAL_SAMPLES - y.size))
    )

    # --- Harmonic / percussive split (§5.1 pitch gate; also the onset source) ---
    try:
        harmonic, percussive = librosa.effects.hpss(y_spec)
        h_energy = float(np.sum(np.square(harmonic, dtype=np.float64)))
        p_energy = float(np.sum(np.square(percussive, dtype=np.float64)))
        total = h_energy + p_energy
        c.harmonic_ratio = float(h_energy / total) if total > 0 else None
    except Exception:  # pragma: no cover - librosa edge cases
        c.harmonic_ratio = None
        percussive = y_spec

    # --- Amplitude envelope, onsets, periodicity, tempo candidates ---
    rms_env = _rms_envelope(y)
    lead_in = np.zeros(_ONSET_LEAD_IN_FRAMES * _HOP, dtype=percussive.dtype)
    padded_env = librosa.onset.onset_strength(
        y=np.concatenate([lead_in, percussive]), sr=sr, hop_length=_HOP, **_SUPERFLUX
    )
    ev.onsets = _dominant_onsets(padded_env, sr, rms_env, _ONSET_LEAD_IN_FRAMES)
    c.onset_count = len(ev.onsets)
    onset_env = padded_env[_ONSET_LEAD_IN_FRAMES:]
    confidence, ac_bpm = _periodicity(onset_env, sr)
    c.tempo_confidence = confidence
    if ac_bpm:
        ev.bpm_candidates.append(ac_bpm)
    if confidence:
        try:
            tempo, _beats = librosa.beat.beat_track(
                onset_envelope=onset_env, sr=sr, hop_length=_HOP
            )
            bt_bpm = float(np.atleast_1d(tempo)[0])
            if bt_bpm > 0:
                ev.bpm_candidates.append(bt_bpm)
        except Exception:  # pragma: no cover - librosa edge cases
            pass

    # --- Amplitude (§5.1) ---
    peak = float(np.max(np.abs(y)))
    rms = float(np.sqrt(np.mean(np.square(y, dtype=np.float64))))
    c.peak_db, c.rms_db = _db_scale(peak), _db_scale(rms)
    c.crest_factor = float(peak / rms) if rms > 0 else None
    c.attack_ms, c.decay_ms = _envelope_times(
        y, sr, ev.onsets[0] if ev.onsets else None, env=rms_env
    )

    # --- Timbre / spectrum (§5.1) ---
    mfcc = librosa.feature.mfcc(y=y_spec, sr=sr, n_mfcc=N_MFCC, hop_length=_HOP)
    c.mfcc_mean = _json_vector(mfcc.mean(axis=1))
    c.mfcc_var = _json_vector(mfcc.var(axis=1))
    contrast = librosa.feature.spectral_contrast(y=y_spec, sr=sr, hop_length=_HOP)
    c.spectral_contrast = _json_vector(contrast.mean(axis=1))
    c.spectral_centroid = float(
        librosa.feature.spectral_centroid(y=y_spec, sr=sr, hop_length=_HOP).mean()
    )
    c.spectral_bandwidth = float(
        librosa.feature.spectral_bandwidth(y=y_spec, sr=sr, hop_length=_HOP).mean()
    )
    c.spectral_rolloff = float(
        librosa.feature.spectral_rolloff(y=y_spec, sr=sr, hop_length=_HOP).mean()
    )
    c.spectral_flatness = float(
        librosa.feature.spectral_flatness(y=y_spec, hop_length=_HOP).mean()
    )

    # --- Pitch, gated on the HPSS harmonic share (§5.1) ---
    if c.harmonic_ratio is not None and c.harmonic_ratio >= PITCH_GATE_HARMONIC_RATIO:
        c.f0_hz, c.pitch_confidence = _pitch(y, sr)
    return c, ev


def load_audio(path: Path | str) -> tuple[np.ndarray, int] | None:
    """Node `B`: decode to ANALYSIS_SR mono. None if it cannot be decoded
    (logged, not fatal — spec §7 node B)."""
    import librosa

    try:
        return librosa.load(str(path), sr=ANALYSIS_SR, mono=True)
    except Exception as exc:
        log.debug("decode failed: %s (%s: %s)", path, type(exc).__name__, exc)
        return None


def analyze_file(path: Path | str) -> Descriptors | None:
    """Nodes `B` + `C` for one file. Returns None if it cannot be decoded."""
    path = Path(path)
    loaded = load_audio(path)
    if loaded is None:
        return None
    y, sr = loaded

    core, evidence = describe_buffer(y, sr)
    d = Descriptors(**asdict(core))
    embedded = read_embedded_metadata(path) or {}
    if embedded:
        d.embedded_metadata_json = json.dumps(embedded, separators=(",", ":"))
    d.key = _embedded_key(embedded)
    if y.size == 0:
        return d

    # --- Loop-ness and tempo (metadata outranks the estimate, §7 node C) ---
    is_loop, tempo_bpm = _loop_decision(
        d,
        y.size / sr,
        embedded,
        evidence.bpm_candidates,
        onsets=evidence.onsets,
        filename=path.name,
    )
    d.is_loop = int(is_loop)
    d.tempo_bpm = tempo_bpm
    return d


def _json_vector(values: np.ndarray) -> str:
    return json.dumps([round(float(v), 6) for v in values.tolist()])


# --- embedded / named metadata ------------------------------------------------------


def _embedded_key(embedded: dict) -> str | None:
    """Pitch class of the ACID root note, when the chunk says it is set.

    Only ACID's `root_set` flag is trusted: across 290 library files with a
    `smpl` chunk, 282 carried the default unity note 60 — a placeholder, not
    information. Key *estimation* (Krumhansl-Schmuckler) is Phase 13 stretch.
    """
    acid = embedded.get("acid") or {}
    note = acid.get("root_note")
    if acid.get("root_set") and isinstance(note, int) and 0 <= note < 128:
        return _PITCH_CLASSES[note % 12]
    return None


def _acid_tempo_bpm(acid: dict, duration_s: float) -> float | None:
    """Authoritative tempo from the ACID chunk: beats over duration.

    The chunk's own `tempo` float is not trustworthy — across 52 ACIDized
    files in the real library every one stored a nominal 120.0 while `beats`
    was correct. Deriving from the beat count reproduces the tempo encoded in
    those files' names exactly (172, 172, 174 BPM), so this is what spec §7
    node C means by embedded metadata being "far more reliable than
    estimating it from audio".
    """
    beats = acid.get("beats") or 0
    if acid.get("one_shot") or beats <= 0 or duration_s <= 0:
        return None
    bpm = beats / duration_s * 60.0
    return bpm if TEMPO_MIN_BPM <= bpm <= TEMPO_MAX_BPM else None


def _filename_tempo_bpm(filename: str) -> float | None:
    """A "<n> BPM" / "<n>bpm" / "<n>_bpm" token in the filename, if any."""
    match = _FILENAME_BPM.search(filename)
    if not match:
        return None
    bpm = float(match.group(1))
    return bpm if TEMPO_MIN_BPM <= bpm <= TEMPO_MAX_BPM else None


# --- loop decision ---------------------------------------------------------------------


def _bar_aligned_tempo(
    duration_s: float, bpm: float, tolerance_beats: float = LOOP_BEAT_TOLERANCE
) -> float | None:
    """The whole-beat tempo nearest `bpm` if the file is a whole number of
    beats (>= LOOP_MIN_BEATS) long at it, within `tolerance_beats`."""
    if bpm <= 0 or duration_s <= 0:
        return None
    beats = duration_s * bpm / 60.0
    n = round(beats)
    if n < LOOP_MIN_BEATS or abs(beats - n) > tolerance_beats:
        return None
    implied = n * 60.0 / duration_s
    return implied if TEMPO_MIN_BPM <= implied <= TEMPO_MAX_BPM else None


def _acoustic_loop_tempo(
    d: Descriptors,
    duration_s: float,
    bpm_candidates: list[float],
    onsets: list[float],
) -> float | None:
    """Whole-beat tempo if the acoustic evidence says "loop", else None.

    Evidence: enough dominant onsets, a genuine periodicity peak, a candidate
    tempo the file is a whole number of beats at, onsets that continue past
    the middle of the file (impacts front-load), and most onsets sitting on
    that tempo's 16th-note grid.

    `bpm_candidates` is [autocorrelation peak, beat_track] (either may be
    missing). The autocorrelation peak is metrically ambiguous — a click every
    0.5 s peaks equally at 0.5 s and 1.0 s — so its octave neighbours are
    tried too, and among the candidates that pass, the one closest to the
    beat tracker's estimate wins: beat_track carries a tempo prior and was the
    more accurate estimator against the library's ACID ground truth.
    """
    if (
        duration_s < LOOP_MIN_DURATION_S
        or d.onset_count < LOOP_MIN_ONSETS
        or (d.tempo_confidence or 0.0) < LOOP_MIN_PERIODICITY
        or not onsets
        or onsets[-1] / duration_s < LOOP_MIN_COVERAGE
        or not bpm_candidates
    ):
        return None
    ac_bpm = bpm_candidates[0]
    bt_bpm = bpm_candidates[1] if len(bpm_candidates) > 1 else None
    trial = [ac_bpm, ac_bpm * 2.0, ac_bpm / 2.0]
    if bt_bpm is not None:
        trial.append(bt_bpm)
    passing = []
    for bpm in trial:
        if not TEMPO_MIN_BPM <= bpm <= TEMPO_MAX_BPM:
            continue
        aligned = _bar_aligned_tempo(duration_s, bpm)
        if aligned is not None and _grid_fit(onsets, aligned) >= LOOP_MIN_GRID_FIT:
            passing.append(aligned)
    if not passing:
        return None
    reference = bt_bpm if bt_bpm is not None else ac_bpm
    return min(passing, key=lambda t: abs(math.log(t / reference)))


def _loop_decision(
    d: Descriptors,
    duration_s: float,
    embedded: dict,
    bpm_candidates: list[float],
    onsets: list[float] | None = None,
    filename: str = "",
) -> tuple[bool, float | None]:
    """(is_loop, tempo_bpm). ACID, then filename tempo, then acoustics.

    `smpl` loop points are deliberately NOT a loop signal: they mark a
    sampler's sustain region on an instrument note (spoken word, single vocal
    notes and synth chords carry them in this library), not tempo-synced
    material.
    """
    acid = embedded.get("acid") or {}
    if acid:
        if acid.get("one_shot"):
            return False, None
        acid_bpm = _acid_tempo_bpm(acid, duration_s)
        if acid_bpm is not None:
            return True, acid_bpm
    named = _filename_tempo_bpm(filename)
    if named is not None and d.onset_count >= 1:
        aligned = _bar_aligned_tempo(duration_s, named, LOOP_BEAT_TOLERANCE_NAMED)
        if aligned is not None:
            return True, aligned
    acoustic = _acoustic_loop_tempo(d, duration_s, bpm_candidates, onsets or [])
    if acoustic is not None:
        return True, acoustic
    if acid.get("stretch") or acid.get("acidized"):
        return True, None  # flagged as ACID loop material, tempo unknowable
    return False, None


def structural_type(
    d: Descriptors,
    duration_s: float,
    one_shot_max_duration_s: float | None = ONE_SHOT_MAX_DURATION_S,
) -> str:
    """Facet B (spec §4): one-shot | multi-hit | loop.

    `one_shot_max_duration_s` is the §9.6 setting: with a value, a single
    dominant onset is a one-shot only up to that length (§4 "short"); with
    None the cap is off and length is ignored.
    """
    if d.is_loop:
        return "loop"
    if d.onset_count <= 1 and (
        one_shot_max_duration_s is None or duration_s <= one_shot_max_duration_s
    ):
        return "one-shot"
    # Several dominant transients without bar-aligned periodicity is a take,
    # not a tempo-synced loop — spec §4 reserves "loop" for periodic material.
    return "multi-hit"


# --- persistence ---------------------------------------------------------------------


_ANALYSIS_COLUMNS = list(Descriptors.__dataclass_fields__)


def _store(
    conn: sqlite3.Connection, sample_id: int, d: Descriptors, facet_b: str
) -> bool:
    """Write the analysis row + Facet B.

    Returns True when a manually-confirmed classification was protected rather
    than overwritten (spec §11).
    """
    d.analyzed_at = now_iso()
    cols = ", ".join(_ANALYSIS_COLUMNS)
    placeholders = ", ".join("?" for _ in _ANALYSIS_COLUMNS)
    row = asdict(d)
    conn.execute(
        f"INSERT OR REPLACE INTO analysis (sample_id, {cols}) "
        f"VALUES (?, {placeholders})",
        [sample_id, *(row[c] for c in _ANALYSIS_COLUMNS)],
    )
    existing = conn.execute(
        "SELECT is_user_confirmed FROM classification WHERE sample_id = ?",
        (sample_id,),
    ).fetchone()
    if existing is not None and existing[0]:
        return True  # manual correction — never overwritten by re-analysis
    conn.execute(
        """
        INSERT INTO classification (sample_id, structural_type, provenance, source_model)
        VALUES (?, ?, 'automatic', 'rules/phase2')
        ON CONFLICT(sample_id) DO UPDATE
            SET structural_type = excluded.structural_type,
                provenance = 'automatic',
                source_model = excluded.source_model
        """,
        (sample_id, facet_b),
    )
    return False


def analyze_pending(
    conn: sqlite3.Connection,
    limit: int | None = None,
    reanalyze: bool = False,
    progress_every: int = 100,
    one_shot_max_duration_s: float | None = ONE_SHOT_MAX_DURATION_S,
    scope: Sequence[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> AnalysisSummary:
    """Analyze samples that are new (no `analysis` row) or stale (content
    changed since `analyzed_at`) — or every sample, if `reanalyze`.

    `one_shot_max_duration_s`: the Facet B duration cap (see structural_type).
    `scope`: folders (the §9.6 folder-scope list); only files under one of
    them are visited. None = everything, the CLI's behaviour.
    `should_stop`: polled before each file (the GUI's Stop button).

    Commits per file: analysis is the expensive stage (§3), so an interrupted
    run must keep everything it already computed.
    """
    summary = AnalysisSummary()
    started = time.perf_counter()

    sql = (
        "SELECT s.id, s.filepath, s.duration_s, "
        "       (a.sample_id IS NOT NULL) AS is_refresh "
        "FROM samples s LEFT JOIN analysis a ON a.sample_id = s.id "
        "WHERE s.duration_s IS NOT NULL"
    )
    if not reanalyze:
        sql += " AND (a.sample_id IS NULL OR s.content_changed_at > a.analyzed_at)"
    scope_sql, params = scope_clause(scope)
    sql += scope_sql + " ORDER BY s.id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    worklist = conn.execute(sql, params).fetchall()
    total = len(worklist)
    log.info("analysis: %d samples to do", total)

    for sample_id, filepath, duration_s, is_refresh in worklist:
        if should_stop is not None and should_stop():
            summary.stopped = True
            log.info("analysis stopped by request after %d of %d", summary.analyzed, total)
            break
        # One file must never take a multi-hour run down with it (spec §7
        # "logged, not fatal"; 2026-09-06 review): decode failures return
        # None, anything else — a librosa edge case, a MemoryError on a very
        # long recording — is caught here, rolled back, counted, and skipped.
        try:
            descriptors = analyze_file(filepath)
            if descriptors is None:
                summary.failed += 1
                if len(summary.error_samples) < 5:
                    summary.error_samples.append(f"decode failed: {filepath}")
                continue
            facet_b = structural_type(
                descriptors, duration_s or 0.0, one_shot_max_duration_s
            )
            protected = _store(conn, sample_id, descriptors, facet_b)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - per-file isolation is the point
            conn.rollback()
            summary.failed += 1
            log.warning("analysis failed: %s (%s: %s)", filepath, type(exc).__name__, exc)
            if len(summary.error_samples) < 5:
                summary.error_samples.append(f"{type(exc).__name__}: {filepath}")
            continue
        if protected:
            summary.skipped_confirmed += 1
        summary.analyzed += 1
        if is_refresh and not reanalyze:
            summary.refreshed_stale += 1
        if progress_every and summary.analyzed % progress_every == 0:
            elapsed = time.perf_counter() - started
            log.info(
                "analyzed %d/%d (%.2f s/file, %d failed)",
                summary.analyzed, total, elapsed / summary.analyzed, summary.failed,
            )

    summary.elapsed_s = time.perf_counter() - started
    return summary
