"""Offscreen tests for the window: the list and its models (Phase 4.5, tree
with sub-hits in Phase 7), the Recompute tab (Phase 8), and Phase 7's
search, anchor, ranking and filters.

Playback itself is not asserted (no audio device in CI); everything else —
rows, sorting, filtering, the segments drill-down, the file URLs that
drag-out hands the OS, the Recompute tab building an index from nothing on
its worker thread, text search with sub-hit rows, anchoring and ranking —
is. Settings go to an INI file under `tmp_path`, never to the user's
registry.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication
from test_embedding import FakeEncoder

from crate.analysis import analyze_pending
from crate.catalog import load_samples, load_segments
from crate.db import open_db
from crate.embedding import embed_pending
from crate.listmodel import ListProxy, SampleTreeModel, SegmentTableModel
from crate.render import render_segment
from crate.scanner import scan_library
from crate.segmentation import segment_pending

SR = 22050
LABELS = {0.4: "kick drum", 0.5: "kick drum", 4.0: "a synth pad"}


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


def _encoder(_settings=None) -> FakeEncoder:
    return FakeEncoder(LABELS)


def _wait_until(app, condition, timeout_s: float = 120.0) -> None:
    """Pump the event loop until `condition()` — worker-thread signals only
    arrive while the loop runs."""
    deadline = time.monotonic() + timeout_s
    while not condition():
        app.processEvents()
        time.sleep(0.02)
        if time.monotonic() > deadline:
            raise TimeoutError("the job did not finish in time")


def _proxy_row_named(window, name: str) -> int:
    return next(
        r for r in range(window._proxy.rowCount())
        if window._proxy.data(window._proxy.index(r, 0)) == name
    )


@pytest.fixture()
def index(tmp_path):
    lib = _write_library(tmp_path / "lib")
    db = tmp_path / "index.db"
    conn = open_db(db)
    scan_library(conn, lib)
    analyze_pending(conn)
    segment_pending(conn)
    embed_pending(conn, encoder=_encoder())
    yield db, conn, tmp_path / "cache"
    conn.close()


def test_sample_model_rows_and_drag_urls(app, index):
    db, conn, cache = index
    model = SampleTreeModel(rows=load_samples(conn))

    assert model.rowCount() == 2 and model.columnCount() == len(SampleTreeModel.COLUMNS)
    files = {model.data(model.index(r, 0)) for r in range(2)}
    assert files == {"hit.wav", "loop.wav"}
    assert model.headerData(2, Qt.Orientation.Horizontal) == "Length"
    assert all(model.rowCount(model.index(r, 0)) == 0 for r in range(2))   # no scores: no sub-hits

    mime = model.mimeData([model.index(0, 0), model.index(0, 3), model.index(1, 0)])
    assert mime.hasUrls()
    urls = [u.toLocalFile() for u in mime.urls()]
    assert len(urls) == 2 and all(u.endswith(".wav") for u in urls)   # one URL per row, not per cell
    assert model.flags(model.index(0, 0)) & Qt.ItemFlag.ItemIsDragEnabled


def test_proxy_sorts_length_as_a_number_and_filters_across_columns(app, index):
    db, conn, cache = index
    model = SampleTreeModel(rows=load_samples(conn))
    proxy = ListProxy()
    proxy.setSourceModel(model)

    proxy.sort(2, Qt.SortOrder.DescendingOrder)               # Length
    assert proxy.data(proxy.index(0, 0)) == "loop.wav"        # 4.0 s before 0.4 s

    proxy.setFilterFixedString("drums")                       # matches the Folder column
    assert proxy.rowCount() == 1 and proxy.data(proxy.index(0, 0)) == "loop.wav"
    proxy.setFilterFixedString("one-shot")                    # matches the Type column
    assert proxy.rowCount() == 1 and proxy.data(proxy.index(0, 0)) == "hit.wav"
    assert proxy.visible_sample_ids() == {model.row_at(model.index(1, 0)).id} or len(proxy.visible_sample_ids()) == 1


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
        assert window._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)
        assert window._table.isColumnHidden(SampleTreeModel.COL_MATCH)

        # Select the loop: its segments appear, the preview target is set.
        window._autoplay.setChecked(False)
        window._table.setCurrentIndex(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))
        assert window._segments.rowCount() > 0
        assert window._current is not None and window._current.name == "loop.wav"
        assert window._attributes._tag_buttons                            # the chips

        window._segment_table.selectRow(0)
        assert window._current is not None and window._current.name.startswith("seg_")
        assert window._current.exists()

        window._filter.setText("hit")
        assert window._proxy.rowCount() == 1
    finally:
        window.close()


# --- Phase 7: search, anchor, ranking, filters (spec §9.4, §9.5, §9.6) ---


def test_text_search_scores_the_list_and_nests_a_sub_hit(app, index, tmp_path):
    from crate.main import MainWindow

    db, conn, cache = index
    window = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path), encoder_factory=_encoder)
    try:
        window._autoplay.setChecked(False)
        window._attributes.search_for("kick drum")
        assert window._search_thread is not None                # embedding runs off the GUI thread
        _wait_until(app, lambda: window._search_thread is None)

        assert not window._table.isColumnHidden(SampleTreeModel.COL_MATCH)
        assert "kick drum" in window.statusBar().currentMessage()
        top = window._proxy.index(0, 0)
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_MATCH)) == "100"
        # The loop's segments are "kick drum" while the loop itself is a pad:
        # the segment shows as a sub-hit row under its parent (§9.4).
        loop = window._proxy.index(_proxy_row_named(window, "loop.wav"), 0)
        assert window._proxy.rowCount(loop) == 1
        sub_hit = window._proxy.index(0, 0, loop)
        assert window._proxy.data(sub_hit).startswith("↳ hit @")
        assert window._proxy.data(window._proxy.index(0, 3, loop)) == "hit"
        assert window._proxy.rowCount(sub_hit) == 0
        del top

        # Selecting the sub-hit previews the rendered segment; dragging it hands out that file.
        window._table.setCurrentIndex(sub_hit)
        assert window._current is not None and window._current.name.startswith("seg_")
        assert window._current_item[0] == "segment"
        mime = window._samples.mimeData([window._proxy.mapToSource(sub_hit)])
        assert [Path(u.toLocalFile()) for u in mime.urls()] == [window._current]

        # The parent inherits its best hit's score for sorting.
        assert window._proxy.data(window._proxy.index(_proxy_row_named(window, "loop.wav"), SampleTreeModel.COL_MATCH)) == "100"

        window._attributes.search_for("kick drum")             # a second query while one is in flight
        window._attributes.search_for("a synth pad")            # ... is replaced by the newest
        _wait_until(app, lambda: window._search_thread is None and "synth pad" in window.statusBar().currentMessage())
        assert window._proxy.data(window._proxy.index(0, 0)) == "loop.wav"

        window._attributes._clear_search()
        assert window._table.isColumnHidden(SampleTreeModel.COL_MATCH)
        assert window._proxy.rowCount(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0)) == 0
    finally:
        window.close()


def test_anchor_unlocks_ranges_and_ranking_and_persists(app, index, tmp_path):
    from crate.main import MainWindow

    db, conn, cache = index
    settings = _ini(tmp_path)
    window = MainWindow(db_path=db, cache_dir=cache, settings=settings, encoder_factory=_encoder)
    try:
        window._autoplay.setChecked(False)
        assert not window._attributes._ranges_group.isEnabled()
        assert not window._recompute._rank_button.isEnabled()

        window._table.setCurrentIndex(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))
        window._anchor_current()

        assert window._anchor_label.text().endswith("loop.wav")
        assert window._attributes._ranges_group.isEnabled()
        assert window._recompute._rank_button.isEnabled()
        assert "loop.wav" in window._recompute._rank_anchor.text()

        window._recompute._rank_button.click()                     # → rank_requested("whole")
        assert not window._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)
        assert window._proxy.data(window._proxy.index(0, 0)) == "loop.wav"   # the anchor itself first
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_SIMILARITY)) == "100"
        assert "ranked 2 samples" in window.statusBar().currentMessage()

        # A narrowed axis range now filters on the anchor distances (§9.5).
        window._attributes._range_max["conceptual"].setValue(10)
        assert window._proxy.rowCount() == 1                       # hit.wav is far in CLAP space
        window._attributes._range_max["conceptual"].setValue(100)
        assert window._proxy.rowCount() == 2

        # Visible-only scope ranks just what the list shows.
        window._filter.setText("hit")
        window._recompute._rank_visible.setChecked(True)
        window._recompute._rank_button.click()
        assert "ranked 1 samples" in window.statusBar().currentMessage()
        window._filter.setText("")

        settings.sync()
        stored = (tmp_path / "crate.ini").read_text(encoding="utf-8")
        assert "anchor" in stored and "kind=sample" in stored
    finally:
        window.close()

    # The anchor survives a restart (§9.4).
    again = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path), encoder_factory=_encoder)
    try:
        assert again._anchor is not None and again._anchor_label.text().endswith("loop.wav")
        assert again._attributes._ranges_group.isEnabled()
        again._clear_anchor()
        assert again._anchor is None and not again._attributes._ranges_group.isEnabled()
    finally:
        again.close()


def test_attribute_filters_apply_to_the_list(app, index, tmp_path):
    from crate.main import MainWindow

    db, conn, cache = index
    window = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path))
    try:
        panel = window._attributes
        assert window._proxy.rowCount() == 2

        panel._type_boxes["one-shot"].setChecked(False)
        assert window._proxy.rowCount() == 1 and window._proxy.data(window._proxy.index(0, 0)) == "loop.wav"
        panel._type_boxes["one-shot"].setChecked(True)

        panel._duration_min.setValue(1.0)
        assert window._proxy.rowCount() == 1
        panel._duration_min.setValue(0.0)

        panel._tempo_min.setValue(100)                      # the loop is 120 BPM; the hit has none
        assert window._proxy.rowCount() == 1 and window._proxy.data(window._proxy.index(0, 0)) == "loop.wav"
        panel._tempo_min.setValue(200)
        assert window._proxy.rowCount() == 0
        panel._tempo_min.setValue(0)
        assert window._proxy.rowCount() == 2

        panel._weight_sliders["pitch"].setValue(30)
        assert panel.weights()["pitch"] == pytest.approx(0.3)
    finally:
        window.close()

    reopened = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path))
    try:
        assert reopened._attributes.weights()["pitch"] == pytest.approx(0.3)   # weights persist (§9.4)
    finally:
        reopened.close()


# --- the Recompute tab (Phase 8, spec §9.6) ---


def test_recompute_tab_builds_the_index_from_the_window(app, tmp_path):
    """Empty index → Rescan → Recompute attributes over a scope, all from the
    window, on the worker thread; the list reloads itself after each job."""
    from crate.main import MainWindow

    lib = _write_library(tmp_path / "lib")
    settings = _ini(tmp_path)
    window = MainWindow(
        db_path=tmp_path / "index.db", cache_dir=tmp_path / "cache",
        settings=settings, encoder_factory=_encoder,
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
