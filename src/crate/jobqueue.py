"""The job queue (Phase 12, 2026-09-13): one worker thread, one connection,
two priorities, preemption between files.

Before this, the Recompute tab ran one job at a time on a thread made for
it, and refused anything else while it ran — a segment save, an anchored
recompute, a caption — with "a job is running". A full recompute of the
library takes hours, so the refusal was the rule, not the exception.

Now every job goes through one queue on one worker thread that owns one
connection — the index keeps its single writer (spec §10) — and jobs come in
two kinds:

* **batch** jobs (a recompute, a rescan, a library-wide layout, a caption
  run): long, and written to call `hooks.after_file(...)` after each file
  they commit. Between two files the worker runs every **interactive** job
  waiting in the queue, on the same thread and connection, then carries on.
  A user's small write waits at most one file's work — about ten seconds on
  the longest file — instead of hours.
* **interactive** jobs (a segment save or delete, an anchored map placement,
  one caption): short, run to completion, never refused.

The hooks also carry progress (stage, done, total, ETA) and the ids of the
files finished since the last report, so the window can show a bar and
refresh those rows in place while the run goes on. Only one batch runs at
a time; a second is refused, as before. Stop applies to the batch and lands
after its current file; interactive jobs finish.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from .db import open_db
from .progress import eta_text

log = logging.getLogger(__name__)

INTERACTIVE = 0
BATCH = 1

Job = Callable[[sqlite3.Connection, Callable[[], bool]], Any]
BatchJob = Callable[[sqlite3.Connection, Callable[[], bool], "JobHooks"], Any]

ROWS_EVERY_S = 2.0          # how often finished rows are handed to the window
ROWS_MAX = 500              # ... or sooner, when this many are waiting
PROGRESS_EVERY_S = 0.5


@dataclass
class QueuedJob:
    name: str
    run: Callable
    batch: bool
    seq: int

    @property
    def priority(self) -> int:
        return BATCH if self.batch else INTERACTIVE


class JobHooks:
    """What a batch job is handed: call `after_file` after every file it
    commits (the worker runs waiting interactive jobs there, reports
    progress, and passes the file's id to the window), or `progress` alone
    for a stage that has no per-file rows to report (the scanner)."""

    def __init__(self, worker: "JobWorker") -> None:
        self._worker = worker

    def after_file(self, stage: str, sample_id: int | None, done: int, total: int) -> None:
        self._worker._after_file(stage, sample_id, done, total)

    def progress(self, stage: str, done: int, total: int) -> None:
        self._worker._report_progress(stage, done, total)


class JobWorker(QThread):
    """The queue and the thread that drains it. Signals cross to the GUI
    thread as queued connections."""

    started_job = Signal(str, bool)            # name, batch
    succeeded = Signal(str, str)               # name, formatted summary
    failed = Signal(str, str)                  # name, error text
    finished_job = Signal(str, bool, bool)     # name, completed (neither failed nor stopped), batch
    progress = Signal(str, int, int, str)      # stage, done, total, ETA text
    rows_done = Signal(list)                   # sample ids finished since the last emit

    def __init__(self, db_path: Path | str, parent=None) -> None:
        super().__init__(parent)
        self._db_path = Path(db_path)
        self._cv = threading.Condition()
        self._queue: list[QueuedJob] = []
        self._seq = 0
        self._closing = False
        self._current: QueuedJob | None = None
        self._batch: QueuedJob | None = None
        self._stop_requested = False
        self._conn: sqlite3.Connection | None = None
        self._hooks = JobHooks(self)
        self._done_ids: list[int] = []
        self._rows_flushed_at = 0.0
        self._progress_at = 0.0
        self._stage_started: dict[str, float] = {}

    # --- the GUI thread's side ---

    def enqueue(self, name: str, run: Callable, *, batch: bool = False) -> None:
        """Queue a job; an interactive one runs ahead of any batch, and
        between the files of a batch already running."""
        with self._cv:
            self._seq += 1
            self._queue.append(QueuedJob(name, run, batch, self._seq))
            self._cv.notify_all()

    @property
    def busy(self) -> bool:
        """A job is running or waiting."""
        with self._cv:
            return self._current is not None or bool(self._queue)

    @property
    def batch_running(self) -> bool:
        return self._batch is not None

    @property
    def batch_queued(self) -> bool:
        with self._cv:
            return self._batch is not None or any(j.batch for j in self._queue)

    def request_stop(self) -> None:
        """Stop the running job: a batch after its current file, an
        interactive job wherever it polls `should_stop` (most do not, and
        finish). Jobs queued behind it start with the flag cleared."""
        self._stop_requested = True

    def shutdown(self) -> None:
        """Stop the batch, drop what is queued, and join the thread."""
        with self._cv:
            self._closing = True
            self._stop_requested = True
            self._queue.clear()
            self._cv.notify_all()
        self.wait()

    # --- the worker thread ---

    def run(self) -> None:  # worker thread
        conn = open_db(self._db_path)
        self._conn = conn
        try:
            while True:
                job = self._take()
                if job is None:
                    break
                self._run_job(job)
        finally:
            self._conn = None
            conn.close()

    def _take(self) -> QueuedJob | None:
        with self._cv:
            while not self._queue and not self._closing:
                self._cv.wait()
            if self._closing:
                return None
            job = min(self._queue, key=lambda j: (j.priority, j.seq))
            self._queue.remove(job)
            self._current = job
            return job

    def _take_interactive(self) -> QueuedJob | None:
        with self._cv:
            waiting = [j for j in self._queue if not j.batch]
            if not waiting or self._closing:
                return None
            job = min(waiting, key=lambda j: j.seq)
            self._queue.remove(job)
            self._current = job
            return job

    def _should_stop(self) -> bool:
        return self._stop_requested or self._closing

    def _run_job(self, job: QueuedJob) -> None:
        """One job, start to end; a batch's `after_file` may run this again
        for an interactive job in the middle of it."""
        assert self._conn is not None
        nested = self._batch is not None           # an interactive job inside a batch's hook
        if job.batch:
            self._batch = job
            self._stage_started.clear()
        self.started_job.emit(job.name, job.batch)
        completed = False
        try:
            if job.batch:
                result = job.run(self._conn, self._should_stop, self._hooks)
            else:
                result = job.run(self._conn, self._should_stop)
        except Exception as exc:  # noqa: BLE001 - reported to the panel, never lost
            log.warning("%s failed: %s: %s", job.name, type(exc).__name__, exc)
            log.debug("job failure", exc_info=True)
            self.failed.emit(job.name, f"{type(exc).__name__}: {exc}")
        else:
            completed = not (job.batch and self._stop_requested)
            text = result.format() if hasattr(result, "format") else str(result)
            self.succeeded.emit(job.name, text)
        finally:
            self._flush_rows(force=True)
            if job.batch:
                self._batch = None
            if not nested:
                # A stop is for the job in flight when it was asked — running,
                # or still queued (the window's close asks before the worker
                # has even taken the job). Consumed here; what comes next
                # starts clean. Inside a batch's hook the batch's stop stands.
                self._stop_requested = False
            with self._cv:
                self._current = self._batch
            self.finished_job.emit(job.name, completed, job.batch)

    # --- hooks, on the worker thread ---

    def _after_file(self, stage: str, sample_id: int | None, done: int, total: int) -> None:
        if sample_id is not None:
            self._done_ids.append(int(sample_id))
        self._report_progress(stage, done, total)
        self._flush_rows()
        while True:
            job = self._take_interactive()
            if job is None:
                return
            self._run_job(job)

    def _report_progress(self, stage: str, done: int, total: int) -> None:
        now = time.monotonic()
        started = self._stage_started.setdefault(stage, now)
        if done < total and now - self._progress_at < PROGRESS_EVERY_S:
            return
        self._progress_at = now
        self.progress.emit(stage, int(done), int(total), eta_text(now - started, done, total))

    def _flush_rows(self, force: bool = False) -> None:
        now = time.monotonic()
        if not self._done_ids:
            return
        if force or len(self._done_ids) >= ROWS_MAX or now - self._rows_flushed_at >= ROWS_EVERY_S:
            ids, self._done_ids = self._done_ids, []
            self._rows_flushed_at = now
            self.rows_done.emit(ids)
