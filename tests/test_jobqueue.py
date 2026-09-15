"""The job queue (Phase 12): one worker, two priorities, preemption between
a batch's files. Offscreen; the jobs here touch no audio."""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from crate.jobqueue import JobWorker


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _Done:
    def __init__(self, text: str) -> None:
        self.text = text

    def format(self) -> str:
        return self.text


def _pump(app, condition, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        app.processEvents()
        time.sleep(0.01)
        if time.monotonic() > deadline:
            raise TimeoutError("the queue did not get there in time")


def test_an_interactive_job_runs_between_a_batchs_files(app, tmp_path):
    """The whole point: a save queued during a five-hour recompute runs at
    the next file boundary, not after the recompute."""
    worker = JobWorker(tmp_path / "q.db")
    worker.start()
    events: list[str] = []
    finished: list[tuple] = []
    rows: list[int] = []
    progress: list[tuple] = []
    worker.finished_job.connect(lambda name, ok, batch: finished.append((name, ok, batch)))
    worker.rows_done.connect(rows.extend)
    worker.progress.connect(lambda *args: progress.append(args))
    gate = threading.Event()                    # the batch holds before its second file

    def batch(conn, stop, hooks):
        for i in range(5):
            if i == 1:
                gate.wait(10)
            events.append(f"file{i}")
            hooks.after_file("describe", 100 + i, i + 1, 5)
        return _Done("batch done")

    def small(conn, stop):
        events.append("small")
        return _Done("small done")

    try:
        worker.enqueue("batch", batch, batch=True)
        _pump(app, lambda: "file0" in events)
        assert worker.busy and worker.batch_running
        worker.enqueue("small", small)
        gate.set()
        _pump(app, lambda: len(finished) == 2)
        assert events[0] == "file0" and events[-1] == "file4"
        assert events.index("small") < events.index("file2"), events
        assert [f[0] for f in finished] == ["small", "batch"] and all(f[1] for f in finished)
        assert finished[0][2] is False and finished[1][2] is True
        assert sorted(rows) == [100, 101, 102, 103, 104]
        assert progress and progress[-1][:3] == ("describe", 5, 5)
        assert not worker.busy and not worker.batch_running
    finally:
        worker.shutdown()


def test_an_interactive_job_runs_ahead_of_a_waiting_batch(app, tmp_path):
    worker = JobWorker(tmp_path / "q.db")
    order: list[str] = []
    hold = threading.Event()

    def blocker(conn, stop):
        hold.wait(10)
        order.append("blocker")
        return _Done("")

    try:
        worker.enqueue("blocker", blocker)       # occupies the worker first
        worker.start()
        _pump(app, lambda: worker.busy)
        worker.enqueue("batch", lambda c, s, h: (order.append("batch"), _Done(""))[1], batch=True)
        worker.enqueue("small", lambda c, s: (order.append("small"), _Done(""))[1])
        assert worker.batch_queued
        hold.set()
        _pump(app, lambda: len(order) == 3)
        assert order == ["blocker", "small", "batch"]
    finally:
        worker.shutdown()


def test_stop_ends_the_batch_after_its_file(app, tmp_path):
    worker = JobWorker(tmp_path / "q.db")
    worker.start()
    finished: list[tuple] = []
    worker.finished_job.connect(lambda n, ok, b: finished.append((n, ok)))
    seen: list[int] = []

    def batch(conn, stop, hooks):
        for i in range(200):
            if stop():
                break
            seen.append(i)
            hooks.after_file("embed", None, i + 1, 200)
            time.sleep(0.02)
        return _Done("stopped" if stop() else "done")

    try:
        worker.enqueue("batch", batch, batch=True)
        _pump(app, lambda: len(seen) >= 3)
        worker.request_stop()
        _pump(app, lambda: bool(finished))
        assert finished == [("batch", False)] and len(seen) < 200
    finally:
        worker.shutdown()


def test_a_failing_job_is_reported_and_the_worker_goes_on(app, tmp_path):
    worker = JobWorker(tmp_path / "q.db")
    worker.start()
    failed: list[tuple] = []
    ok: list[tuple] = []
    worker.failed.connect(lambda n, e: failed.append((n, e)))
    worker.succeeded.connect(lambda n, t: ok.append((n, t)))

    def bad(conn, stop):
        raise RuntimeError("boom")

    try:
        worker.enqueue("bad", bad)
        worker.enqueue("good", lambda c, s: _Done("fine"))
        _pump(app, lambda: bool(ok))
        assert failed == [("bad", "RuntimeError: boom")] and ok == [("good", "fine")]
    finally:
        worker.shutdown()
