"""One pass per file — nodes B, C, S, T and C2 together (Phase 12, 2026-09-13).

*Recompute attributes* ran analysis over every file and then segmentation
over every file: two decodes and two HPSS splits per file, and a full set of
transforms again for every segment window. Measured on the user's library
(`.docs/phase12_profile.md`), HPSS alone was 7.4 of ~10 s per long file —
run twice — and the second decode another 0.8 s; on short multi-hits the
per-window transforms were up to a third of the time. Worker processes were
already at their useful limit (8 of 32 cores was the fastest count), so the
saving had to be algorithmic.

This stage decodes once, computes the frame features once
(`analysis.frame_features`), and reads everything off them: the file's
descriptors and structural type, its transients on the same percussive
component, and every segment's descriptors from the frames inside its
window (`analysis.describe_window`). The two stages' summaries come back as
before, so the report, the tests and the CLI's separate `crate-analyze` /
`crate-segment` are unchanged; the stand-alone stages share the same frame
path, so their numbers agree with this one.

What node S sees for the detection: a type the user confirmed (Phase 11)
wins; otherwise the type this pass just derived, or the index's for a file
whose analysis is current. Nothing here runs on its own (§9.6).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .analysis import (
    ONE_SHOT_MAX_DURATION_S,
    AnalysisSummary,
    Descriptors,
    _store,
    analyze_loaded,
    describe_window,
    frame_features,
    load_audio,
    structural_type,
)
from .db import ids_clause, now_iso, scope_clause
from .progress import eta_text
from .segmentation import (
    DetectionResult,
    SegmentationSettings,
    SegmentationSummary,
    SegmentWork,
    _apply_manual_refresh,
    _clear_auto_segments,
    _manual_rows,
    _stamp_parent,
    apply_segment_work,
    detect_loaded,
)

log = logging.getLogger(__name__)


@dataclass
class FileWork:
    """Everything one file's pass computed, off the main process; the index
    is written from it on the main thread (`_apply`)."""

    descriptors: Descriptors | None        # None when the analysis was current
    facet_b: str | None                    # the rule's type — what `_store` writes unless protected
    structural_type: str                   # what the detection used
    one_shot: bool
    segments: SegmentWork | None           # detection + window descriptors; None for a one-shot
    manual: list = field(default_factory=list)   # a stale one-shot's manual segments, re-described
    duration_ms: int = 0


def describe_file(
    filepath: str,
    duration_s: float | None,
    needs_analysis: bool,
    needs_segments: bool,
    type_hint: str | None,
    type_confirmed: bool,
    content_class: str | None,
    settings: SegmentationSettings,
    one_shot_max_duration_s: float | None = ONE_SHOT_MAX_DURATION_S,
    analyze_segments: bool = True,
    manual_rows: Sequence[tuple[int, int, int]] = (),
) -> FileWork | None:
    """The whole pass for one file — pure, so a worker process can run it.
    None if the file cannot be decoded."""
    loaded = load_audio(filepath)
    if loaded is None:
        return None
    y, sr = loaded
    frames = frame_features(y, sr) if y.size else None
    descriptors = facet_b = None
    if needs_analysis:
        descriptors = analyze_loaded(filepath, y, sr, frames)
        facet_b = structural_type(descriptors, duration_s or 0.0, one_shot_max_duration_s)
    if type_confirmed and type_hint:
        kind = type_hint                               # the user's call (Phase 11) — node S reads the index
    elif facet_b is not None:
        kind = facet_b
    else:
        kind = type_hint or "multi-hit"
    duration_ms = int(round(y.size / sr * 1000.0)) if sr else 0
    work = FileWork(descriptors, facet_b, kind, kind == "one-shot", None, duration_ms=duration_ms)
    if not needs_segments:
        return work
    if work.one_shot:
        # A one-shot has nothing inside to find; a stale one's manual
        # segments are re-described against the new audio, as node T's are.
        work.manual = [
            (seg_id, start_ms, end_ms,
             describe_window(frames, y, sr, start_ms, end_ms) if analyze_segments and frames is not None else None)
            for seg_id, start_ms, end_ms in manual_rows
        ]
    else:
        work.segments = detect_loaded(
            y, sr, kind, settings, content_class, analyze_segments, manual_rows, frames
        )
    return work


def describe_pending(
    conn: sqlite3.Connection,
    settings: SegmentationSettings | None = None,
    *,
    full: bool = False,
    one_shot_max_duration_s: float | None = ONE_SHOT_MAX_DURATION_S,
    analyze_segments: bool = True,
    scope: Sequence[str] | None = None,
    sample_ids: Sequence[int] | None = None,
    should_stop: Callable[[], bool] | None = None,
    workers: int = 1,
    progress_every: int = 100,
    order: Sequence[int] | None = None,
    after_file: Callable[[str, int | None, int, int], None] | None = None,
) -> tuple[AnalysisSummary, SegmentationSummary]:
    """Analysis and segmentation over every sample that needs either — one
    decode and one set of frame features per file. `full` redoes both for
    every sample in scope. Returns the two stages' summaries, as the report
    and the tests have always read them.

    `order`: sample ids in the order to visit them — the list's, top to
    bottom, so what the user is looking at is done first (the job queue,
    Phase 12); ids not in it come last, in id order. `after_file(stage,
    sample_id, done, total)` is called after each file is committed — where
    the queue runs waiting interactive jobs and reports progress."""
    settings = settings or SegmentationSettings()
    analysis = AnalysisSummary()
    segmentation = SegmentationSummary()
    started = time.perf_counter()
    run_started_at = now_iso()

    needs_analysis_sql = "(an.sample_id IS NULL OR s.content_changed_at > an.analyzed_at)"
    needs_segments_sql = "(s.segments_detected_at IS NULL OR s.segments_detected_at < s.content_changed_at)"
    sql = (
        "SELECT s.id, s.filepath, s.duration_s, "
        f"       {needs_analysis_sql} AS needs_analysis, "
        "       (an.sample_id IS NOT NULL) AS is_refresh, "
        f"       {needs_segments_sql} AS needs_segments, "
        "       (s.segments_detected_at IS NOT NULL "
        "        AND s.segments_detected_at < s.content_changed_at) AS seg_stale, "
        "       k.structural_type, COALESCE(k.structural_type_confirmed, 0) AS type_confirmed, k.content_class "
        "FROM samples s "
        "LEFT JOIN analysis an ON an.sample_id = s.id "
        "LEFT JOIN classification k ON k.sample_id = s.id "
        "WHERE s.duration_s IS NOT NULL"
    )
    if not full:
        sql += f" AND ({needs_analysis_sql} OR {needs_segments_sql})"
    scope_sql, params = scope_clause(scope)
    ids_sql, ids_params = ids_clause(sample_ids)
    sql += scope_sql + ids_sql + " ORDER BY s.id"
    params += ids_params
    worklist = conn.execute(sql, params).fetchall()
    if order:
        rank = {int(sid): i for i, sid in enumerate(order)}
        worklist.sort(key=lambda row: rank.get(int(row["id"]), len(rank)))
    total = len(worklist)
    log.info("describing %d samples: analysis + segmentation in one pass per file", total)

    def flags(row) -> tuple[bool, bool, bool]:
        needs_a = full or bool(row["needs_analysis"])
        needs_s = full or bool(row["needs_segments"])
        refresh = bool(row["seg_stale"])           # manual segments re-described only when the content changed
        return needs_a, needs_s, refresh

    def args_of(row) -> tuple:
        needs_a, needs_s, refresh = flags(row)
        manual = _manual_rows(conn, int(row["id"])) if needs_s and refresh else ()
        return (
            row["filepath"], row["duration_s"], needs_a, needs_s,
            row["structural_type"], bool(row["type_confirmed"]), row["content_class"],
            settings, one_shot_max_duration_s, analyze_segments, manual,
        )

    def note_failure(row, needs_a: bool, needs_s: bool, message: str) -> None:
        for summary in ((analysis,) if needs_a else ()) + ((segmentation,) if needs_s else ()):
            summary.failed += 1
            if len(summary.error_samples) < 5:
                summary.error_samples.append(message)

    done = 0

    def apply(row, work: FileWork | None, error: Exception | None) -> None:
        """Write one file's pass on the main thread — the index's only
        writer — with the same per-file isolation the stages had: one bad
        file is counted and rolled back, never fatal."""
        nonlocal done
        sample_id, filepath = int(row["id"]), row["filepath"]
        needs_a, needs_s, refresh = flags(row)
        if error is not None:
            conn.rollback()
            log.warning("describe failed: %s (%s: %s)", filepath, type(error).__name__, error)
            note_failure(row, needs_a, needs_s, f"{type(error).__name__}: {filepath}")
        elif work is None:
            conn.rollback()
            note_failure(row, needs_a, needs_s, f"decode failed: {filepath}")
        else:
            try:
                if needs_a and work.descriptors is not None:
                    if _store(conn, sample_id, work.descriptors, work.facet_b):
                        analysis.skipped_confirmed += 1
                    analysis.analyzed += 1
                    if bool(row["is_refresh"]) and not full:
                        analysis.refreshed_stale += 1
                if needs_s:
                    if work.one_shot:
                        _clear_auto_segments(conn, sample_id)
                        if refresh:
                            segmentation.manual_flagged += _apply_manual_refresh(conn, work.duration_ms, work.manual)
                        _stamp_parent(conn, sample_id, DetectionResult(), now_iso())
                        segmentation.one_shots_skipped += 1
                    elif work.segments is not None:
                        result = apply_segment_work(conn, sample_id, work.segments, refresh_manual=refresh)
                        segmentation.samples_segmented += 1
                        segmentation.segments_created += result.rows_inserted
                        segmentation.manual_flagged += result.manual_flagged
                        if result.capped:
                            segmentation.samples_capped += 1
                        if not result.segments:
                            segmentation.samples_without_segments += 1
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - per-file isolation is the point
                conn.rollback()
                log.warning("describe failed: %s (%s: %s)", filepath, type(exc).__name__, exc)
                note_failure(row, needs_a, needs_s, f"{type(exc).__name__}: {filepath}")
        done += 1
        if progress_every and done % progress_every == 0:
            elapsed = time.perf_counter() - started
            log.info(
                "described %d/%d (%.2f s/file, %d analysed, %d segments, %d failed, %s)",
                done, total, elapsed / done, analysis.analyzed, segmentation.segments_created,
                analysis.failed + segmentation.failed, eta_text(elapsed, done, total),
            )
        if after_file is not None:
            after_file("describe", sample_id, done, total)

    def stopped() -> None:
        analysis.stopped = segmentation.stopped = True
        log.info("describe stopped by request after %d of %d", done, total)

    if workers > 1 and worklist:
        from .parallel import BoundedMap, make_pool

        log.info("describe: %d worker processes", workers)
        with make_pool(workers) as pool:
            mapping = BoundedMap(pool, worklist, describe_file, args_of, should_stop)
            for row, work, error in mapping:
                apply(row, work, error)
        if mapping.stopped:
            stopped()
    else:
        for row in worklist:
            if should_stop is not None and should_stop():
                stopped()
                break
            try:
                work, error = describe_file(*args_of(row)), None
            except Exception as exc:  # noqa: BLE001 - reported through apply()
                work, error = None, exc
            apply(row, work, error)

    segmentation.manual_kept = conn.execute(
        "SELECT COUNT(*) FROM segments g JOIN samples s ON s.id = g.sample_id "
        "WHERE (g.detection_method = 'manual' OR g.is_user_confirmed = 1) "
        "  AND s.segments_detected_at >= ?",
        (run_started_at,),
    ).fetchone()[0]
    analysis.elapsed_s = segmentation.elapsed_s = time.perf_counter() - started
    return analysis, segmentation
