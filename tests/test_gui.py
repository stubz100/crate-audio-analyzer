"""Offscreen tests for the window: the "listen and grab" list and its models
(Phase 4.5) and the Recompute tab (Phase 8).

Playback itself is not asserted (no audio device in CI); everything else —
rows, sorting, filtering, the segments drill-down, the file URLs that
drag-out hands the OS, and the Recompute tab building an index from nothing
on its worker thread — is. Settings go to an INI file under `tmp_path`, never
to the user's registry.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QSettings, QSortFilterProxyModel, Qt
from PySide6.QtWidgets import QApplication
from test_embedding import FakeEncoder

from crate.analysis import analyze_pending
from crate.catalog import load_samples, load_segments
from crate.db import open_db
from crate.listmodel import SORT_ROLE, SampleTableModel, SegmentTableModel
from crate.render import render_segment
from crate.scanner import scan_library
from crate.segmentation import segment_pending

SR = 22050


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _clicks(times_s, duration_s):
    y = np.zeros(int(SR * duration_s))
    burst = int(0.02 * SR)
    for i, t in enumerate(times_s):
        s = int(t * SR)
        y[s : s + burst] += np.random.default_rng(i).normal(0, 0.5, burst)
    return np.clip(y, -1, 1).astype("float32")


def _write_library(root):
    (root / "Drums").mkdir(parents=True)
    sf.write(root / "Drums" / "loop.wav", _clicks([i * 0.5 for i in range(8)], 4.0), SR)
    sf.write(root / "hit.wav", _clicks([0.0], 0.4), SR)
    return root


def _ini(tmp_path) -> QSettings:
    return QSettings(str(tmp_path / "crate.ini"), QSettings.Format.IniFormat)


def _wait_until(app, condition, timeout_s: float = 120.0) -> None:
    """Pump the event loop until `condition()` — worker-thread signals only
    arrive while the loop runs."""
    deadline = time.monotonic() + timeout_s
    while not condition():
        app.processEvents()
        time.sleep(0.02)
        if time.monotonic() > deadline:
            raise TimeoutError("the job did not finish in time")


@pytest.fixture()
def index(tmp_path):
    lib = _write_library(tmp_path / "lib")
    db = tmp_path / "index.db"
    conn = open_db(db)
    scan_library(conn, lib)
    analyze_pending(conn)
    segment_pending(conn)
    yield db, conn, tmp_path / "cache"
    conn.close()


def test_sample_model_rows_and_drag_urls(app, index):
    db, conn, cache = index
    model = SampleTableModel(load_samples(conn))

    assert model.rowCount() == 2 and model.columnCount() == len(SampleTableModel.COLUMNS)
    files = {model.data(model.index(r, 0)) for r in range(2)}
    assert files == {"hit.wav", "loop.wav"}
    assert model.headerData(2, Qt.Orientation.Horizontal) == "Length"

    mime = model.mimeData([model.index(0, 0), model.index(0, 3), model.index(1, 0)])
    assert mime.hasUrls()
    urls = [u.toLocalFile() for u in mime.urls()]
    assert len(urls) == 2 and all(u.endswith(".wav") for u in urls)   # one URL per row, not per cell
    assert model.flags(model.index(0, 0)) & Qt.ItemFlag.ItemIsDragEnabled


def test_proxy_sorts_length_as_a_number_and_filters_across_columns(app, index):
    db, conn, cache = index
    model = SampleTableModel(load_samples(conn))
    proxy = QSortFilterProxyModel()
    proxy.setSourceModel(model)
    proxy.setSortRole(SORT_ROLE)
    proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    proxy.setFilterKeyColumn(-1)

    proxy.sort(2, Qt.SortOrder.DescendingOrder)               # Length
    assert proxy.data(proxy.index(0, 0)) == "loop.wav"        # 4.0 s before 0.4 s

    proxy.setFilterFixedString("drums")                       # matches the Folder column
    assert proxy.rowCount() == 1 and proxy.data(proxy.index(0, 0)) == "loop.wav"
    proxy.setFilterFixedString("one-shot")                    # matches the Type column
    assert proxy.rowCount() == 1 and proxy.data(proxy.index(0, 0)) == "hit.wav"


def test_segment_model_drag_renders_the_segment_first(app, index):
    db, conn, cache = index
    loop_id = conn.execute("SELECT id FROM samples WHERE filename = 'loop.wav'").fetchone()[0]
    rendered: list[int] = []

    def render(segment_id):
        rendered.append(segment_id)
        return render_segment(conn, segment_id, cache)

    model = SegmentTableModel(render)
    model.set_rows(load_segments(conn, loop_id))
    assert model.rowCount() > 0

    mime = model.mimeData([model.index(0, 0), model.index(0, 1)])

    assert rendered == [model.row_at(model.index(0, 0)).id]  # once per row, not per cell
    urls = [u.toLocalFile() for u in mime.urls()]
    assert len(urls) == 1 and urls[0].endswith(f"seg_{rendered[0]}.wav")
    assert os.path.exists(urls[0])


def test_main_window_loads_the_index_and_drills_into_segments(app, index, tmp_path):
    from crate.main import MainWindow

    db, conn, cache = index
    window = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path))
    try:
        assert window._proxy.rowCount() == 2
        assert "2 samples" in window.statusBar().currentMessage()

        # Select the loop: its segments appear, the preview target is set.
        loop_row = next(
            r for r in range(window._proxy.rowCount())
            if window._proxy.data(window._proxy.index(r, 0)) == "loop.wav"
        )
        window._autoplay.setChecked(False)
        window._table.selectRow(loop_row)
        assert window._segments.rowCount() > 0
        assert window._current is not None and window._current.name == "loop.wav"

        window._segment_table.selectRow(0)
        assert window._current is not None and window._current.name.startswith("seg_")
        assert window._current.exists()

        window._filter.setText("hit")
        assert window._proxy.rowCount() == 1
    finally:
        window.close()


# --- the Recompute tab (Phase 8, spec §9.6) ---


def test_recompute_tab_builds_the_index_from_the_window(app, tmp_path):
    """Empty index → Rescan → Recompute attributes over a scope, all from the
    window, on the worker thread; the list reloads itself after each job."""
    from crate.main import MainWindow

    lib = _write_library(tmp_path / "lib")
    settings = _ini(tmp_path)
    window = MainWindow(
        db_path=tmp_path / "index.db", cache_dir=tmp_path / "cache",
        settings=settings, encoder_factory=lambda _embed_settings: FakeEncoder(),
    )
    try:
        panel = window._recompute
        assert window._proxy.rowCount() == 0 and not panel.running

        panel.set_library_root(lib)
        panel.run_rescan()
        assert panel.running
        _wait_until(app, lambda: not panel.running)
        assert window._proxy.rowCount() == 2                     # reloaded itself
        assert "added 2" in panel.log_text()

        panel.run_recompute()                                    # empty scope: refused
        assert not panel.running and "scope is empty" in panel.log_text()

        panel.add_scope_folder(lib / "Drums")
        panel.run_recompute()
        _wait_until(app, lambda: not panel.running)
        status = window.statusBar().currentMessage()
        assert "· 1 analysed" in status and "· 1 embedded" in status   # the loop only
        assert "[embedding]" in panel.log_text()

        panel.add_scope_folder(lib)
        panel.run_recompute()
        _wait_until(app, lambda: not panel.running)
        assert "· 2 analysed" in window.statusBar().currentMessage()

        settings.sync()
        stored = (tmp_path / "crate.ini").read_text(encoding="utf-8")
        assert "Drums" in stored and "root_path" in stored          # scope + root persisted
    finally:
        window.close()


def test_recompute_settings_round_trip_and_validation(app, tmp_path):
    from crate.recompute import RecomputePanel

    settings = _ini(tmp_path)
    panel = RecomputePanel(tmp_path / "index.db", settings)
    panel._force_full.setChecked(True)
    panel._sensitivity.setValue(0.4)
    panel._max_segments.setValue(8)
    panel._one_shot_cap.setChecked(False)
    panel._embed_segments.setChecked(False)
    panel.add_scope_folder(tmp_path / "a_b")

    collected = panel.collect_settings()
    assert collected.force_full and collected.scope == (str(tmp_path / "a_b"),)
    assert collected.segmentation.sensitivity == 0.4 and collected.segmentation.max_segments == 8
    assert collected.one_shot_max_duration_s is None and not collected.embedding.embed_segments

    panel.save_settings()
    settings.sync()
    again = RecomputePanel(tmp_path / "index.db", _ini(tmp_path))
    assert again.collect_settings() == collected

    again._min_length.setValue(5.0)                               # min ≥ max: refused,
    again._max_length.setValue(1.0)                               # nothing starts
    again.run_recompute()
    assert not again.running and "settings:" in again.log_text()

    again.stop()                                                  # no job: a no-op
    assert not again.running


def test_closing_the_window_mid_job_waits_for_the_job(app, tmp_path):
    """Close while a job runs: the window stays, the job is asked to stop,
    and the window closes itself when the job ends — never a reload on a
    closed connection, never a thread destroyed under its job."""
    from crate.main import MainWindow

    window = MainWindow(
        db_path=tmp_path / "index.db", cache_dir=tmp_path / "cache", settings=_ini(tmp_path)
    )
    window.show()
    panel = window._recompute

    class Done:
        def format(self):
            return "slow job done"

    def slow_job(conn, should_stop):
        while not should_stop():
            time.sleep(0.01)
        time.sleep(0.2)                                   # "the current file" after the stop
        return Done()

    panel._start("slow job", slow_job)
    assert panel.running

    assert not window.close() and window.isVisible()      # refused: the job is still running
    assert "stop requested" in panel.log_text()

    _wait_until(app, lambda: not window.isVisible(), timeout_s=30)
    assert not panel.running and "slow job done" in panel.log_text()
