"""The Recompute tab's engine (spec §9.6, Phase 8) — no Qt.

*Rescan library* is `scanner.scan_library` as it is. *Recompute attributes*
is `recompute_attributes` below: analysis → segmentation → embedding over
the folder-scope list, either new/changed only (what the scanner flagged)
or a forced full re-index — or, *⚓ anchored sample only* (§9.6's fast
loop, 2026-09-08), every stage again for the one anchored sample. The
GUI worker and the tests call exactly this,
so what the button does is what the test checks. Nothing here runs unless
something calls it (§9.6: on demand only).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .analysis import ONE_SHOT_MAX_DURATION_S, AnalysisSummary, analyze_pending
from .embedding import ClapEncoder, EmbedSettings, EmbedSummary, Encoder, embed_pending
from .segmentation import SegmentationSettings, SegmentationSummary, segment_pending

log = logging.getLogger(__name__)


@dataclass
class RecomputeSettings:
    """What the Recompute tab's controls amount to (§9.6)."""

    scope: tuple[str, ...] = ()          # the folder-scope list: absolute folders
    force_full: bool = False             # "new/changed only" vs "force full re-index"
    sample_ids: tuple[int, ...] = ()     # ⚓ anchored only: these samples (the anchor's parent) and nothing
                                         # else — the folder scope is not consulted, every stage redoes them
    one_shot_max_duration_s: float | None = ONE_SHOT_MAX_DURATION_S
    workers: int = 1                     # worker processes for analysis + segmentation (parallel.py)
    segmentation: SegmentationSettings = field(default_factory=SegmentationSettings)
    embedding: EmbedSettings = field(default_factory=EmbedSettings)


@dataclass
class RecomputeReport:
    """One run's three stage summaries. A stage that did not run is None —
    because an earlier one was stopped, or (embedding) the model stack is
    not installed, which `notes` explains."""

    analysis: AnalysisSummary | None = None
    segmentation: SegmentationSummary | None = None
    embedding: EmbedSummary | None = None
    notes: list[str] = field(default_factory=list)
    stopped: bool = False
    elapsed_s: float = 0.0

    def format(self) -> str:
        lines: list[str] = []
        stages = (
            ("analysis", self.analysis),
            ("segmentation", self.segmentation),
            ("embedding", self.embedding),
        )
        for name, summary in stages:
            if summary is None:
                lines.append(f"[{name}] not run")
                continue
            lines.append(f"[{name}]")
            lines.extend("  " + line for line in summary.format().splitlines())
        lines.extend(f"note: {note}" for note in self.notes)
        if self.stopped:
            lines.append("stopped by request; everything done so far is kept")
        lines.append(f"total elapsed: {self.elapsed_s:.1f}s")
        return "\n".join(lines)


def recompute_attributes(
    conn: sqlite3.Connection,
    settings: RecomputeSettings,
    should_stop: Callable[[], bool] | None = None,
    encoder: Encoder | None = None,
    progress_every: int = 25,
) -> RecomputeReport:
    """§9.6 *Recompute attributes*: the three stages in order over the files
    under `settings.scope` — or, with `settings.sample_ids`, over those
    samples alone (*⚓ anchored sample only*: no folder needed, every stage
    again under the settings as they are, one worker — seconds, not hours).

    Refuses an empty scope — there is deliberately no implicit "everything"
    (§9.6); the root folder itself is how the whole library is chosen. A
    stop ends the current stage after its current file and skips the rest;
    what was committed stays. A missing model stack (the `ml` extra) skips
    embedding with a note rather than failing the run: Phases 1–3 are
    useful on their own.
    """
    anchored = tuple(int(i) for i in settings.sample_ids)
    if not anchored and not settings.scope:
        raise ValueError(
            "the library scope is empty: add a folder to it "
            "(the library root itself for the whole library)"
        )
    report = RecomputeReport()
    started = time.perf_counter()
    # Anchored only: the anchor is the scope, and every stage redoes it under
    # the settings as they are — the point is to see a changed setting on the
    # one file in front of you — and one file needs no worker pool.
    scope = None if anchored else list(settings.scope)
    full = settings.force_full or bool(anchored)
    workers = 1 if anchored else settings.workers
    sample_ids = anchored or None
    if anchored:
        marks = ",".join("?" for _ in anchored)
        names = [row[0] for row in conn.execute(f"SELECT filepath FROM samples WHERE id IN ({marks})", anchored)]
        log.info("recompute attributes (anchor only): every stage again for %s", ", ".join(names) or anchored)
    else:
        mode = "force full re-index" if settings.force_full else "new/changed only"
        log.info(
            "recompute attributes (%s) over %d scope folder(s), %d worker process(es)",
            mode, len(scope), max(1, workers),
        )
        for folder in scope:
            log.info("  scope: %s", folder)

    report.analysis = analyze_pending(
        conn,
        reanalyze=full,
        progress_every=progress_every,
        one_shot_max_duration_s=settings.one_shot_max_duration_s,
        scope=scope,
        sample_ids=sample_ids,
        should_stop=should_stop,
        workers=workers,
    )
    if report.analysis.stopped:
        return _finish(report, started, stopped=True)

    report.segmentation = segment_pending(
        conn,
        settings=settings.segmentation,
        resegment=full,
        progress_every=progress_every,
        scope=scope,
        sample_ids=sample_ids,
        should_stop=should_stop,
        workers=workers,
    )
    if report.segmentation.stopped:
        return _finish(report, started, stopped=True)

    try:
        encoder = encoder or ClapEncoder(
            settings.embedding.checkpoint, settings.embedding.batch_size
        )
        report.embedding = embed_pending(
            conn,
            encoder=encoder,
            settings=settings.embedding,
            reembed=full,
            progress_every=progress_every,
            scope=scope,
            sample_ids=sample_ids,
            should_stop=should_stop,
        )
    except ImportError as exc:
        note = (
            f"embedding skipped: the CLAP model stack is not installed ({exc}); "
            "run `uv sync --extra ml` (spec §12, Phase 4)"
        )
        log.warning(note)
        report.notes.append(note)
        return _finish(report, started)
    except Exception as exc:  # noqa: BLE001 - a failed model load must not hide the stages that ran
        note = (
            f"embedding stage failed ({type(exc).__name__}: {exc}); everything analysis "
            "and segmentation committed is kept — fix the cause and run again with "
            "'new/changed only'"
        )
        log.warning(note)
        log.debug("embedding stage failure", exc_info=True)
        report.notes.append(note)
        return _finish(report, started)
    return _finish(report, started, stopped=report.embedding.stopped)


def _finish(report: RecomputeReport, started: float, stopped: bool = False) -> RecomputeReport:
    report.stopped = stopped
    report.elapsed_s = time.perf_counter() - started
    return report
