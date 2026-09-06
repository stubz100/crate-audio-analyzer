"""Offscreen tests for the "listen and grab" window and its models (Phase 4.5).

Playback itself is not asserted (no audio device in CI); everything else —
rows, sorting, filtering, the segments drill-down, and the file URLs that
drag-out hands the OS — is.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtWidgets import QApplication

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


@pytest.fixture()
def index(tmp_path):
    lib = tmp_path / "lib"
    (lib / "Drums").mkdir(parents=True)
    sf.write(lib / "Drums" / "loop.wav", _clicks([i * 0.5 for i in range(8)], 4.0), SR)
    sf.write(lib / "hit.wav", _clicks([0.0], 0.4), SR)
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


def test_main_window_loads_the_index_and_drills_into_segments(app, index):
    from crate.main import MainWindow

    db, conn, cache = index
    window = MainWindow(db_path=db, cache_dir=cache)
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
