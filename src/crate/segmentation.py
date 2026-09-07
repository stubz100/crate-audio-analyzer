"""Transient segmentation — pipeline nodes `S` and `T` (spec §7), Phase 3.

Node `S` is a cheap gate: a clean single-hit one-shot has nothing inside it to
find, so it is routed straight past segmentation. Everything else (multi-hit
takes, loops) goes to node `T`, which detects transient boundaries and writes
`segments` rows, plus the per-segment descriptor pass node `C2` needs.

What a segment *is* (spec §6.4): a start/end marker pair indexing into its
parent, never a row in `samples`. Callers that list or map samples can ignore
these tables entirely.

The four §6.2 controls are all here and all configurable (§9.6):

* **Profile** — how onsets are detected. *Tight* is superflux spectral flux on
  the HPSS percussive component: sharp, transient-focused, right for drum and
  loop material. *Loose* is half-wave-rectified energy flux on the full mix
  with onset merging, for gestural multi-transient takes whose "hits" swell
  rather than click. `auto` picks tight for loops and loose for multi-hit
  material; when Facet A lands in Phase 4, Rhythmic content should select
  tight as well (spec §6.2 names both).
* **Sensitivity** — the strength a transient needs, as a fraction of the
  strongest transient in the file, to count as a candidate at all.
* **Boundary mode** — how a segment *ends*, orthogonal to the profile.
  Transient-to-transient stops at the next candidate onset (breakbeat-chop
  style, the default); transient-to-fixed-length runs on for the max length
  regardless of what happens inside, for gestures that should stay whole.
* **Length rules and the cap** — too short is dropped as noise, too long is
  **truncated rather than dropped** (an obvious transient still yields a
  segment), and at most `max_segments` survive per sample, strongest first.
  When the cap actually binds the parent is flagged so the UI can show §6.2's
  "capped" warning instead of silently losing hits.

Two protections this module must never violate:

* **Manual segments are exempt from every rule above** (§6.3) — no length
  check, no cap, no truncation — and automatic re-detection never touches
  them. Only an explicit delete removes one.
* **Nothing runs on its own** (§9.6). Detection is an explicit `crate-segment`
  run; a changed file is *flagged* stale by the scanner and re-segmented on
  the next such run, never in the background.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from .analysis import (
    _HOP,
    _ONSET_LEAD_IN_FRAMES,
    _SUPERFLUX,
    CoreDescriptors,
    describe_buffer,
    load_audio,
)
from .db import now_iso, scope_clause

log = logging.getLogger(__name__)

PROFILE_AUTO = "auto"
PROFILE_TIGHT = "tight"
PROFILE_LOOSE = "loose"
PROFILES = (PROFILE_AUTO, PROFILE_TIGHT, PROFILE_LOOSE)

BOUNDARY_TRANSIENT = "transient-to-transient"
BOUNDARY_FIXED = "transient-to-fixed-length"
BOUNDARY_MODES = (BOUNDARY_TRANSIENT, BOUNDARY_FIXED)

UNIT_SECONDS = "s"
UNIT_PERCENT = "%"
UNITS = (UNIT_SECONDS, UNIT_PERCENT)

# Defaults. Spec §9.6 marks sensitivity and the length rules "(tuning pass
# expected)" — these are starting points measured against the real library,
# not settled values.
DEFAULT_SENSITIVITY = 0.15      # of the strongest transient in the file
DEFAULT_MIN_LENGTH_S = 0.05     # below this a "segment" is a detector artefact
DEFAULT_MAX_LENGTH_S = 2.0
DEFAULT_MAX_SEGMENTS = 5        # spec §6.2

_LOOSE_MERGE_GAP_S = 0.08       # loose profile only: onsets closer than this merge
_RMS_FRAME = 2048               # energy envelope window for the loose profile
                                # and for backtracking, at _HOP
_MAX_BACKTRACK_FRAMES = 2       # ~46 ms at _HOP. Backtracking to the preceding energy
                                # minimum keeps a segment from clipping its own attack,
                                # but the minimum can sit far back on material that
                                # decays slowly — measured up to 128 ms, far enough to
                                # swallow the *previous* hit's tail. The detected onset
                                # already trails the true attack by a frame or two, so
                                # a couple of frames of look-back is all that is wanted.


@dataclass
class SegmentationSettings:
    """The §6.2 / §9.6 controls. Length rules take seconds or a percentage of
    the parent's duration, which is why each carries its own unit."""

    profile: str = PROFILE_AUTO
    sensitivity: float = DEFAULT_SENSITIVITY
    boundary_mode: str = BOUNDARY_TRANSIENT
    min_length: float = DEFAULT_MIN_LENGTH_S
    min_length_unit: str = UNIT_SECONDS
    max_length: float = DEFAULT_MAX_LENGTH_S
    max_length_unit: str = UNIT_SECONDS
    max_segments: int = DEFAULT_MAX_SEGMENTS

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"profile must be one of {PROFILES}: {self.profile!r}")
        if self.boundary_mode not in BOUNDARY_MODES:
            raise ValueError(f"boundary_mode must be one of {BOUNDARY_MODES}")
        if self.min_length_unit not in UNITS or self.max_length_unit not in UNITS:
            raise ValueError(f"length units must be one of {UNITS}")
        if not 0.0 <= self.sensitivity <= 1.0:
            raise ValueError("sensitivity is a fraction of the strongest transient (0..1)")
        if self.max_segments < 1:
            raise ValueError("max_segments must be at least 1")
        if self.min_length <= 0 or self.max_length <= 0:
            raise ValueError("length rules must be positive")
        if self.min_length_unit == self.max_length_unit and self.min_length >= self.max_length:
            raise ValueError("min_length must be below max_length (every segment would be dropped)")

    @staticmethod
    def _resolve(value: float, unit: str, duration_s: float) -> float:
        return duration_s * value / 100.0 if unit == UNIT_PERCENT else value

    def min_length_s(self, duration_s: float) -> float:
        return self._resolve(self.min_length, self.min_length_unit, duration_s)

    def max_length_s(self, duration_s: float) -> float:
        return self._resolve(self.max_length, self.max_length_unit, duration_s)


@dataclass
class SegmentCandidate:
    """One detected segment, before it becomes a row."""

    start_s: float
    end_s: float
    strength: float  # 0..1, relative to the strongest transient in the parent

    @property
    def start_ms(self) -> int:
        return int(round(self.start_s * 1000.0))

    @property
    def end_ms(self) -> int:
        return int(round(self.end_s * 1000.0))


@dataclass
class DetectionResult:
    segments: list[SegmentCandidate] = field(default_factory=list)
    candidates_found: int = 0          # survived the length rules, before the cap
    capped: bool = False
    effective_sensitivity: float | None = None
    profile: str = PROFILE_TIGHT
    rows_inserted: int = 0             # set by segment_sample: rows actually written
    manual_flagged: int = 0            # set by segment_sample: manual segments now
                                       # ending past the (changed) file


@dataclass
class SegmentationSummary:
    samples_segmented: int = 0
    one_shots_skipped: int = 0   # node S: nothing inside to find; stale auto rows cleared
    segments_created: int = 0    # rows written, not candidates proposed
    samples_capped: int = 0
    samples_without_segments: int = 0
    manual_kept: int = 0         # manual segments under the samples this run touched
    manual_flagged: int = 0      # ... of which now end past their changed parent
    failed: int = 0
    stopped: bool = False        # stopped by request; what was done is kept
    elapsed_s: float = 0.0
    error_samples: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            f"segmented {self.samples_segmented} samples | "
            f"{self.segments_created} segments | "
            f"capped {self.samples_capped} | "
            f"no segments found {self.samples_without_segments} | "
            f"one-shots skipped {self.one_shots_skipped} | "
            f"manual kept {self.manual_kept} | failed {self.failed}"
        ]
        if self.manual_flagged:
            lines.append(
                f"manual segments needing review: {self.manual_flagged} "
                f"(parent content changed; they now end past the file)"
            )
        lines.extend(f"  ! {s}" for s in self.error_samples)
        if self.stopped:
            lines.append("stopped by request; everything segmented so far is kept")
        lines.append(f"elapsed: {self.elapsed_s:.1f}s")
        return "\n".join(lines)


# --- node S: the gate -----------------------------------------------------------
#
# Node `S` (spec §7) is the question "is there anything inside this file worth
# finding?", and its answer is `classification.structural_type != 'one-shot'`,
# read from the index in `segment_pending`. There is deliberately no second,
# recomputed version of that test here: the stored type already reflects the
# duration cap that was in force at analysis time AND any manual correction
# (§11), and a recomputation would honour neither (2026-09-06 review).


def choose_profile(
    settings: SegmentationSettings,
    structural_type_value: str,
    content_class: str | None = None,
) -> str:
    """Resolve `auto` against the parent's types (spec §6.2: tight for
    Rhythmic/Loop content, loose for the rest). Facet A arrives in Phase 4 and
    is NULL until then, or when flagged low-confidence."""
    if settings.profile != PROFILE_AUTO:
        return settings.profile
    if structural_type_value == "loop" or content_class == "rhythmic":
        return PROFILE_TIGHT
    return PROFILE_LOOSE


# --- node T: detection ------------------------------------------------------------


def _percussive(y: np.ndarray) -> np.ndarray:
    import librosa

    try:
        return librosa.effects.hpss(y)[1]
    except Exception:  # pragma: no cover - librosa edge cases
        return y


def _envelopes(
    y: np.ndarray, sr: int, profile: str
) -> tuple[np.ndarray, np.ndarray]:
    """(onset envelope, energy envelope) for `profile`, both at `_HOP`.

    Both are computed on audio with `_ONSET_LEAD_IN_FRAMES` of silence
    prepended: a transient at sample 0 otherwise collides with the frame
    centering pad and is never picked (measured in Phase 2).
    """
    import librosa

    lead = np.zeros(_ONSET_LEAD_IN_FRAMES * _HOP, dtype=y.dtype)
    source = np.concatenate([lead, _percussive(y) if profile == PROFILE_TIGHT else y])
    energy = librosa.feature.rms(y=source, frame_length=_RMS_FRAME, hop_length=_HOP)[0]
    if profile == PROFILE_TIGHT:
        onset_env = librosa.onset.onset_strength(
            y=source, sr=sr, hop_length=_HOP, **_SUPERFLUX
        )
    else:
        # Energy-based: half-wave-rectified first difference of the RMS
        # envelope. A gesture swells instead of clicking, so spectral flux
        # under-reports it while a rise in energy still marks its start.
        onset_env = np.maximum(0.0, np.diff(energy, prepend=energy[0]))
    return onset_env, energy


def _merge_close(
    transients: list[tuple[float, float]], gap_s: float
) -> list[tuple[float, float]]:
    """Collapse onsets closer than `gap_s`, keeping the strongest of each run
    (the loose profile's "onset-merging", spec §6.2)."""
    merged: list[tuple[float, float]] = []
    for time_s, strength in transients:
        if merged and time_s - merged[-1][0] < gap_s:
            if strength > merged[-1][1]:
                merged[-1] = (merged[-1][0], strength)
            continue
        merged.append((time_s, strength))
    return merged


def detect_transients(
    y: np.ndarray, sr: int, settings: SegmentationSettings, profile: str
) -> list[tuple[float, float]]:
    """Candidate transients as (time_s, normalized strength), in time order.

    Onsets are backtracked to the preceding energy minimum so a segment starts
    just before its attack rather than clipping it — this is a slicer's cut
    point, not a beat position.
    """
    import librosa

    if y.size == 0:
        return []
    onset_env, energy = _envelopes(y, sr, profile)
    if onset_env.size == 0 or not np.any(onset_env > 0):
        return []
    frames = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=sr, hop_length=_HOP, units="frames"
    )
    if len(frames) == 0:
        return []
    strengths = onset_env[frames]
    strongest = float(strengths.max())
    if strongest <= 0:
        return []

    # Backtracking is for the tight profile only. Superflux peaks *after* the
    # attack, so its cut point has to be walked back to the preceding energy
    # minimum. The loose profile's envelope is the rise of a windowed RMS,
    # which already crosses zero before the attack — backtracking it as well
    # just drags the start into the previous hit.
    if profile == PROFILE_TIGHT:
        backtracked = librosa.onset.onset_backtrack(frames, energy)
    else:
        backtracked = frames

    duration_s = y.size / sr
    transients: list[tuple[float, float]] = []
    for frame, cut, strength in zip(frames, backtracked, strengths):
        if float(strength) < settings.sensitivity * strongest:
            continue
        # Bound the look-back, then clamp into the file: backtracking may also
        # cross the prepended lead-in.
        cut = max(int(cut), int(frame) - _MAX_BACKTRACK_FRAMES)
        start_s = max(0.0, (cut - _ONSET_LEAD_IN_FRAMES) * _HOP / sr)
        if start_s >= duration_s:
            continue
        transients.append((start_s, float(strength) / strongest))
    transients.sort(key=lambda t: t[0])
    if profile == PROFILE_LOOSE:
        transients = _merge_close(transients, _LOOSE_MERGE_GAP_S)
    return transients


def build_segments(
    transients: list[tuple[float, float]],
    duration_s: float,
    settings: SegmentationSettings,
) -> DetectionResult:
    """Turn candidate transients into bounded, capped segments (spec §6.2)."""
    result = DetectionResult()
    if not transients or duration_s <= 0:
        return result
    min_len = settings.min_length_s(duration_s)
    max_len = settings.max_length_s(duration_s)

    kept: list[SegmentCandidate] = []
    for index, (start_s, strength) in enumerate(transients):
        if settings.boundary_mode == BOUNDARY_FIXED:
            end_s = start_s + max_len
        elif index + 1 < len(transients):
            end_s = transients[index + 1][0]
        else:
            end_s = duration_s
        end_s = min(end_s, duration_s)
        if end_s - start_s > max_len:
            end_s = start_s + max_len       # truncate, never drop (§6.2)
        if end_s - start_s < min_len:
            continue                        # too short: noise, dropped (§6.2)
        kept.append(SegmentCandidate(start_s, end_s, strength))

    result.candidates_found = len(kept)
    if len(kept) > settings.max_segments:
        result.capped = True
        strongest_first = sorted(kept, key=lambda s: -s.strength)[: settings.max_segments]
        result.effective_sensitivity = min(s.strength for s in strongest_first)
        kept = sorted(strongest_first, key=lambda s: s.start_s)
    result.segments = kept
    return result


def detect(
    y: np.ndarray,
    sr: int,
    settings: SegmentationSettings,
    structural_type_value: str = "multi-hit",
    content_class: str | None = None,
) -> DetectionResult:
    """Node `T` end to end on one decoded buffer."""
    profile = choose_profile(settings, structural_type_value, content_class)
    transients = detect_transients(y, sr, settings, profile)
    result = build_segments(transients, y.size / sr if sr else 0.0, settings)
    result.profile = profile
    return result


# --- persistence -------------------------------------------------------------------

# The segment table mirrors `analysis` minus the file-level columns (spec §8)
# and minus `tempo_confidence`; derived from CoreDescriptors so a new
# descriptor reaches segments without a name list to remember.
_SEGMENT_DESCRIPTOR_FIELDS = tuple(
    name for name in CoreDescriptors.__dataclass_fields__ if name != "tempo_confidence"
)


def _write_segment_analysis(
    conn: sqlite3.Connection,
    segment_id: int,
    y: np.ndarray,
    sr: int,
    start_ms: int,
    end_ms: int,
) -> None:
    """Node `C2`'s descriptor half on one window (the CLAP half is Phase 4).

    `tempo_bpm` stays NULL: a segment is a one-shot by construction (§6.4) and
    the Phase 2 policy is that tempo is stored only for loops.
    """
    _store_segment_analysis(conn, segment_id, _describe_window(y, sr, start_ms, end_ms))


def _describe_window(y: np.ndarray, sr: int, start_ms: int, end_ms: int):
    """The descriptor half of node `C2` on one window — pure, so a worker
    process can do it (`detect_file`)."""
    start = max(0, int(start_ms * sr / 1000))
    end = min(y.size, int(end_ms * sr / 1000))
    core, _evidence = describe_buffer(y[start:end], sr)
    return core


def _store_segment_analysis(conn: sqlite3.Connection, segment_id: int, core) -> None:
    columns = ("analyzed_at",) + _SEGMENT_DESCRIPTOR_FIELDS
    values = [now_iso()] + [getattr(core, name) for name in _SEGMENT_DESCRIPTOR_FIELDS]
    conn.execute(
        f"INSERT OR REPLACE INTO segment_analysis (segment_id, {', '.join(columns)}) "
        f"VALUES (?, {', '.join('?' for _ in columns)})",
        [segment_id, *values],
    )


def _describe_segment(
    conn: sqlite3.Connection, segment_id: int, filepath: str, start_ms: int, end_ms: int
) -> None:
    """Decode the parent and describe one window; a decode failure leaves the
    segment without descriptors (logged at DEBUG by load_audio), never raises."""
    loaded = load_audio(filepath)
    if loaded is not None:
        y, sr = loaded
        _write_segment_analysis(conn, segment_id, y, sr, start_ms, end_ms)


def _clear_auto_segments(conn: sqlite3.Connection, sample_id: int) -> None:
    """Drop a sample's automatic segments; manual/confirmed ones are untouched (§6.3)."""
    conn.execute(
        "DELETE FROM segments WHERE sample_id = ? AND detection_method = 'auto' "
        "AND is_user_confirmed = 0",
        (sample_id,),
    )


def _stamp_parent(
    conn: sqlite3.Connection, sample_id: int, result: DetectionResult, now: str
) -> None:
    conn.execute(
        "UPDATE samples SET segment_candidates_found = ?, segments_capped = ?, "
        "effective_sensitivity = ?, segments_detected_at = ? WHERE id = ?",
        (result.candidates_found, int(result.capped), result.effective_sensitivity,
         now, sample_id),
    )


def _refresh_manual_segments(
    conn: sqlite3.Connection,
    sample_id: int,
    y: np.ndarray,
    sr: int,
    analyze_segments: bool,
) -> int:
    """Manual segments after their parent's content changed (2026-09-06 review).

    Their bounds are never touched (§6.3) — but their descriptors were computed
    on the old audio and any cached render is of the old audio, so both are
    redone; a segment that now ends past the file is flagged `needs_review`
    instead of silently indexing air. Returns the number flagged.
    """
    duration_ms = int(round(y.size / sr * 1000.0))
    manual = [
        (seg_id, start_ms, end_ms, _describe_window(y, sr, start_ms, end_ms) if analyze_segments else None)
        for seg_id, start_ms, end_ms in _manual_rows(conn, sample_id)
    ]
    return _apply_manual_refresh(conn, duration_ms, manual)


def _manual_rows(conn: sqlite3.Connection, sample_id: int) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        (int(r[0]), int(r[1]), int(r[2]))
        for r in conn.execute(
            "SELECT id, start_ms, end_ms FROM segments WHERE sample_id = ? "
            "AND (detection_method = 'manual' OR is_user_confirmed = 1)",
            (sample_id,),
        )
    )


def _apply_manual_refresh(conn: sqlite3.Connection, duration_ms: int, manual) -> int:
    """Write the refresh of manual segments: the review flag, the cleared
    render and vector, and the descriptors computed on the new audio."""
    flagged = 0
    for segment_id, start_ms, end_ms, core in manual:
        out_of_range = start_ms >= duration_ms or end_ms > duration_ms
        flagged += int(out_of_range)
        conn.execute(
            "UPDATE segments SET needs_review = ?, cache_path = NULL, "
            "cache_rendered_at = NULL WHERE id = ?",
            (int(out_of_range), segment_id),
        )
        # Its vector was of the old audio too: drop it, crate-embed refills it.
        conn.execute("DELETE FROM segment_embedding WHERE segment_id = ?", (segment_id,))
        if core is not None:
            _store_segment_analysis(conn, segment_id, core)
    return flagged


def segment_sample(
    conn: sqlite3.Connection,
    sample_id: int,
    filepath: str,
    structural_type_value: str,
    settings: SegmentationSettings,
    analyze_segments: bool = True,
    refresh_manual: bool = False,
    content_class: str | None = None,
) -> DetectionResult | None:
    """Node `T` for one sample: detect, replace its automatic segments, and
    record the §6.2 counters on the parent. Returns None if it cannot decode.

    Manual segments are never replaced (§6.3). With `refresh_manual` — the
    driver passes it when the parent's content changed — their descriptors and
    review flag are brought up to date against the new audio.
    """
    manual_rows = _manual_rows(conn, sample_id) if refresh_manual else ()
    work = detect_file(
        filepath, structural_type_value, settings, content_class, analyze_segments, manual_rows
    )
    if work is None:
        return None
    return apply_segment_work(conn, sample_id, work, refresh_manual)


@dataclass
class SegmentWork:
    """Everything node `T` computes for one file without touching the index
    — so `detect_file` can run in a worker process (`parallel.py`) and
    `apply_segment_work` writes it here."""

    result: DetectionResult
    descriptors: list                         # per candidate: CoreDescriptors | None
    duration_ms: int
    manual: list                              # (id, start_ms, end_ms, CoreDescriptors | None)


def detect_file(
    filepath: str,
    structural_type_value: str,
    settings: SegmentationSettings,
    content_class: str | None = None,
    analyze_segments: bool = True,
    manual_rows=(),
) -> SegmentWork | None:
    """Decode, detect, and describe every window (the candidates and the
    manual segments handed in) — pure; None if the file cannot decode."""
    loaded = load_audio(filepath)
    if loaded is None:
        return None
    y, sr = loaded
    result = detect(y, sr, settings, structural_type_value, content_class)
    descriptors = [
        _describe_window(y, sr, c.start_ms, c.end_ms) if analyze_segments else None
        for c in result.segments
    ]
    manual = [
        (seg_id, start_ms, end_ms, _describe_window(y, sr, start_ms, end_ms) if analyze_segments else None)
        for seg_id, start_ms, end_ms in manual_rows
    ]
    return SegmentWork(result, descriptors, int(round(y.size / sr * 1000.0)), manual)


def apply_segment_work(
    conn: sqlite3.Connection, sample_id: int, work: SegmentWork, refresh_manual: bool
) -> DetectionResult:
    """Write one file's `SegmentWork`: replace its automatic segments, store
    their descriptors, refresh manual ones, stamp the parent (§6.2)."""
    result = work.result
    _clear_auto_segments(conn, sample_id)
    now = now_iso()
    for candidate, core in zip(result.segments, work.descriptors):
        cursor = conn.execute(
            "INSERT OR IGNORE INTO segments "
            "(sample_id, start_ms, end_ms, detection_method, is_user_confirmed, "
            " strength, detected_at) VALUES (?, ?, ?, 'auto', 0, ?, ?)",
            (sample_id, candidate.start_ms, candidate.end_ms, candidate.strength, now),
        )
        if not cursor.rowcount:
            continue  # duplicate rounded bounds, or a window that rounds to 0 ms
        result.rows_inserted += 1
        if core is not None:
            _store_segment_analysis(conn, int(cursor.lastrowid), core)
    if refresh_manual:
        result.manual_flagged = _apply_manual_refresh(conn, work.duration_ms, work.manual)
    _stamp_parent(conn, sample_id, result, now)
    return result


def _skip_one_shot(
    conn: sqlite3.Connection,
    sample_id: int,
    filepath: str,
    stale: bool,
    analyze_segments: bool,
) -> int:
    """Node `S` says no: a clean one-shot has nothing inside to find.

    Automatic segments it may still carry from a previous life as a loop are
    cleared (2026-09-06 review: they used to linger forever), manual ones are
    refreshed if the content changed, and the sample is stamped so it is not
    revisited until its content changes again. Returns manual segments flagged.
    """
    _clear_auto_segments(conn, sample_id)
    flagged = 0
    has_manual = conn.execute(
        "SELECT 1 FROM segments WHERE sample_id = ? LIMIT 1", (sample_id,)
    ).fetchone()
    if stale and has_manual:
        loaded = load_audio(filepath)
        if loaded is not None:
            flagged = _refresh_manual_segments(conn, sample_id, *loaded, analyze_segments)
    _stamp_parent(conn, sample_id, DetectionResult(), now_iso())
    return flagged


def segment_pending(
    conn: sqlite3.Connection,
    settings: SegmentationSettings | None = None,
    limit: int | None = None,
    resegment: bool = False,
    analyze_segments: bool = True,
    progress_every: int = 100,
    scope: Sequence[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    workers: int = 1,
) -> SegmentationSummary:
    """Run nodes `S` + `T` over samples that need it.

    Node `S` reads `classification.structural_type` — the DB's, not a
    recomputation, so a manual correction to one-shot (§11) is honored. By
    default only samples never segmented, or whose content changed since they
    were (the scanner's staleness flag), are visited; one-shots among them are
    cleared and stamped rather than detected.

    `scope` (folders, §9.6) limits the visit to files under them; None is
    everything. `should_stop` is polled before each sample. `workers` above 1
    fans the decode + detection out to worker processes (`parallel.py`);
    the index is still written here.
    """
    settings = settings or SegmentationSettings()
    summary = SegmentationSummary()
    started = time.perf_counter()
    run_started_at = now_iso()

    sql = (
        "SELECT s.id, s.filepath, k.structural_type, k.content_class, "
        "       (s.segments_detected_at IS NOT NULL "
        "        AND s.segments_detected_at < s.content_changed_at) AS is_stale "
        "FROM samples s JOIN classification k ON k.sample_id = s.id "
        "WHERE s.duration_s IS NOT NULL AND k.structural_type IS NOT NULL"
    )
    if not resegment:
        sql += (
            " AND (s.segments_detected_at IS NULL "
            "      OR s.segments_detected_at < s.content_changed_at)"
        )
    scope_sql, params = scope_clause(scope)
    sql += scope_sql + " ORDER BY s.id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    worklist = conn.execute(sql, params).fetchall()
    total = len(worklist)
    log.info("segmentation: %d samples to visit", total)

    def finish(row, result, error) -> None:
        """Count one file's outcome. Per-sample isolation (2026-09-06 review):
        one exotic file must not take the run down — a decode failure is
        `result is None`, anything else arrives as `error`, rolled back."""
        filepath = row[1]
        if error is not None:
            conn.rollback()
            summary.failed += 1
            log.warning("segmentation failed: %s (%s: %s)", filepath, type(error).__name__, error)
            if len(summary.error_samples) < 5:
                summary.error_samples.append(f"{type(error).__name__}: {filepath}")
            return
        if result is None:
            conn.rollback()
            summary.failed += 1
            if len(summary.error_samples) < 5:
                summary.error_samples.append(f"decode failed: {filepath}")
            return
        summary.samples_segmented += 1
        summary.segments_created += result.rows_inserted
        summary.manual_flagged += result.manual_flagged
        if result.capped:
            summary.samples_capped += 1
        if not result.segments:
            summary.samples_without_segments += 1
        if progress_every and summary.samples_segmented % progress_every == 0:
            elapsed = time.perf_counter() - started
            log.info(
                "segmented %d/%d (%.2f s/sample, %d segments)",
                summary.samples_segmented, total,
                elapsed / summary.samples_segmented, summary.segments_created,
            )

    def stop_now() -> bool:
        if should_stop is not None and should_stop():
            summary.stopped = True
            log.info(
                "segmentation stopped by request after %d of %d",
                summary.samples_segmented + summary.one_shots_skipped, total,
            )
            return True
        return False

    # Node S first: a one-shot has nothing inside to find, costs no detection,
    # and stays in this process; the rest is node T's decode + detect.
    to_detect = []
    for row in worklist:
        sample_id, filepath, structural_type_value, content_class, is_stale = row
        if structural_type_value != "one-shot":
            to_detect.append(row)
            continue
        if stop_now():
            break
        try:
            summary.manual_flagged += _skip_one_shot(
                conn, sample_id, filepath, bool(is_stale), analyze_segments
            )
            conn.commit()
            summary.one_shots_skipped += 1
        except Exception as exc:  # noqa: BLE001 - per-file isolation is the point
            finish(row, None, exc)

    if not summary.stopped and workers > 1 and to_detect:
        from .parallel import BoundedMap, make_pool

        def args_of(row):
            sample_id, filepath, structural_type_value, content_class, is_stale = row
            manual = _manual_rows(conn, sample_id) if is_stale else ()
            return (filepath, structural_type_value, settings, content_class, analyze_segments, manual)

        log.info("segmentation: %d worker processes", workers)
        with make_pool(workers) as pool:
            mapping = BoundedMap(pool, to_detect, detect_file, args_of, should_stop)
            for row, work, error in mapping:
                result = None
                if error is None and work is not None:
                    try:
                        result = apply_segment_work(conn, row[0], work, refresh_manual=bool(row[4]))
                        conn.commit()
                    except Exception as exc:  # noqa: BLE001
                        error = exc
                finish(row, result, error)
        if mapping.stopped:
            summary.stopped = True
            log.info(
                "segmentation stopped by request after %d of %d",
                summary.samples_segmented + summary.one_shots_skipped, total,
            )
    elif not summary.stopped:
        for row in to_detect:
            if stop_now():
                break
            sample_id, filepath, structural_type_value, content_class, is_stale = row
            result, error = None, None
            try:
                result = segment_sample(
                    conn, sample_id, filepath, structural_type_value, settings,
                    analyze_segments, refresh_manual=bool(is_stale),
                    content_class=content_class,
                )
                if result is not None:
                    conn.commit()
            except Exception as exc:  # noqa: BLE001
                error = exc
            finish(row, result, error)

    # One query, not one per sample: manual segments under everything this
    # run stamped (all stamps are >= run_started_at).
    summary.manual_kept = conn.execute(
        "SELECT COUNT(*) FROM segments g JOIN samples s ON s.id = g.sample_id "
        "WHERE (g.detection_method = 'manual' OR g.is_user_confirmed = 1) "
        "  AND s.segments_detected_at >= ?",
        (run_started_at,),
    ).fetchone()[0]
    summary.elapsed_s = time.perf_counter() - started
    return summary


# --- manual segments (spec §6.3) ------------------------------------------------------


def create_manual_segment(
    conn: sqlite3.Connection,
    sample_id: int,
    start_ms: int,
    end_ms: int,
    analyze_segment: bool = True,
) -> int:
    """Save a hand-placed segment. Exempt from every §6.2 constraint — no
    length check, no cap, no truncation — and protected from automatic
    overwrite from this point on."""
    if end_ms <= start_ms:
        raise ValueError(f"end_ms must be after start_ms ({start_ms} -> {end_ms})")
    row = conn.execute(
        "SELECT filepath FROM samples WHERE id = ?", (sample_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no sample with id {sample_id}")
    cursor = conn.execute(
        "INSERT INTO segments (sample_id, start_ms, end_ms, detection_method, "
        "is_user_confirmed, detected_at) VALUES (?, ?, ?, 'manual', 1, ?)",
        (sample_id, int(start_ms), int(end_ms), now_iso()),
    )
    segment_id = int(cursor.lastrowid)
    if analyze_segment:
        _describe_segment(conn, segment_id, row[0], int(start_ms), int(end_ms))
    conn.commit()
    return segment_id


def update_segment(
    conn: sqlite3.Connection,
    segment_id: int,
    start_ms: int | None = None,
    end_ms: int | None = None,
    analyze_segment: bool = True,
) -> None:
    """Adjust a segment's markers by hand.

    Adjusting *anything* makes it a manual, protected segment (§6.3 covers
    "create or adjust"), so a hand-nudged automatic segment survives the next
    detection run. Any cached render is dropped: the bounds it was rendered
    from no longer hold (§6.5).
    """
    row = conn.execute(
        "SELECT sg.start_ms, sg.end_ms, s.filepath FROM segments sg "
        "JOIN samples s ON s.id = sg.sample_id WHERE sg.id = ?",
        (segment_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no segment with id {segment_id}")
    new_start = int(start_ms if start_ms is not None else row[0])
    new_end = int(end_ms if end_ms is not None else row[1])
    if new_end <= new_start:
        raise ValueError(f"end_ms must be after start_ms ({new_start} -> {new_end})")
    conn.execute(
        "UPDATE segments SET start_ms = ?, end_ms = ?, detection_method = 'manual', "
        "is_user_confirmed = 1, needs_review = 0, cache_path = NULL, "
        "cache_rendered_at = NULL, detected_at = ? WHERE id = ?",
        (new_start, new_end, now_iso(), segment_id),
    )
    # New bounds, new window: the stored vector no longer describes it.
    conn.execute("DELETE FROM segment_embedding WHERE segment_id = ?", (segment_id,))
    if analyze_segment:
        _describe_segment(conn, segment_id, row[2], new_start, new_end)
    conn.commit()


def delete_segment(conn: sqlite3.Connection, segment_id: int) -> bool:
    """Delete a segment, automatic or manual. §6.3's protection is against
    silent automatic overwrite, never against a deliberate delete."""
    cursor = conn.execute("DELETE FROM segments WHERE id = ?", (segment_id,))
    conn.commit()
    return bool(cursor.rowcount)


def segments_for(conn: sqlite3.Connection, sample_id: int) -> list[sqlite3.Row]:
    """A sample's segments, in time order — the §6.4 drill-down view."""
    return conn.execute(
        "SELECT * FROM segments WHERE sample_id = ? ORDER BY start_ms", (sample_id,)
    ).fetchall()
