"""The Recompute tab's engine (spec §9.6, Phase 8) — no Qt.

*Rescan library* is `scanner.scan_library` as it is. *Recompute attributes*
is `recompute_attributes` below: analysis → segmentation → embedding over
the folder-scope list, either new/changed only (what the scanner flagged)
or a forced full re-index. The GUI worker and the tests call exactly this,
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
    one_shot_max_duration_s: float | None = ONE_SHOT_MAX_DURATION_S
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
    """§9.6 *Recompute attributes*, library scope: the three stages in order
    over the files under `settings.scope`.

    Refuses an empty scope — there is deliberately no implicit "everything"
    (§9.6); the root folder itself is how the whole library is chosen. A
    stop ends the current stage after its current file and skips the rest;
    what was committed stays. A missing model stack (the `ml` extra) skips
    embedding with a note rather than failing the run: Phases 1–3 are
    useful on their own.
    """
    if not settings.scope:
        raise ValueError(
            "the library scope is empty: add a folder to it "
            "(the library root itself for the whole library)"
        )
    report = RecomputeReport()
    started = time.perf_counter()
    scope = list(settings.scope)
    mode = "force full re-index" if settings.force_full else "new/changed only"
    log.info("recompute attributes (%s) over %d scope folder(s)", mode, len(scope))
    for folder in scope:
        log.info("  scope: %s", folder)

    report.analysis = analyze_pending(
        conn,
        reanalyze=settings.force_full,
        progress_every=progress_every,
        one_shot_max_duration_s=settings.one_shot_max_duration_s,
        scope=scope,
        should_stop=should_stop,
    )
    if report.analysis.stopped:
        return _finish(report, started, stopped=True)

    report.segmentation = segment_pending(
        conn,
        settings=settings.segmentation,
        resegment=settings.force_full,
        progress_every=progress_every,
        scope=scope,
        should_stop=should_stop,
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
            reembed=settings.force_full,
            progress_every=progress_every,
            scope=scope,
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
