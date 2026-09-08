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
from crate.layout import PcaReducer
from crate.library import add_library, normalize
from crate.listmodel import ListProxy, SampleTreeModel, SegmentTableModel
from crate.recompute import RunPlan
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


def _run_ranking(window) -> None:
    """Press Run on the Recompute tab with only the Ranking step ticked —
    ranking runs on the GUI thread, so it is done when this returns."""
    panel = window._recompute
    panel._step_attributes.setChecked(False)
    panel._step_layout.setChecked(False)
    panel._step_ranking.setChecked(True)
    panel.run()


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
        assert window._attributes._strip.dimensions == 32                 # the fake model's vector
        assert window._waveform.loaded and window._waveform.duration_s == pytest.approx(4.0, abs=0.01)
        assert window._attributes.isAncestorOf(window._segment_table)     # the table lives in the tab

        window._segment_table.selectRow(0)
        assert window._current is not None and window._current.name.startswith("seg_")
        assert window._current.exists()
        first = window._segments.row_at(window._segments.index(0, 0)).id
        assert window._waveform._selected_segment == first               # mirrored on the waveform
        assert window._current_offset_ms == window._segments.row_at(window._segments.index(0, 0)).start_ms

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
        assert window._segments.rowCount() > 0                  # the drill-down shows the parent's segments
        assert window._attributes._tag_buttons                    # ... and the chips are the parent's
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
        assert not window._recompute._step_ranking.isEnabled()

        window._table.setCurrentIndex(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))
        window._anchor_current()

        assert window._anchor_label.text().endswith("loop.wav")
        assert window._attributes._ranges_group.isEnabled()
        assert window._recompute._step_ranking.isEnabled()
        assert "loop.wav" in window._recompute._rank_note.text()

        for slider in window._attributes._weight_sliders.values():
            slider.setValue(0)
        _run_ranking(window)                                       # nothing to blend: told, not silent
        assert "weight" in window.statusBar().currentMessage()
        assert window._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)
        for slider in window._attributes._weight_sliders.values():
            slider.setValue(100)

        _run_ranking(window)                                       # Run with only Ranking ticked
        assert not window._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)
        assert window._proxy.data(window._proxy.index(0, 0)) == "loop.wav"   # the anchor itself first
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_SIMILARITY)) == "100"
        assert "ranked 2 samples" in window.statusBar().currentMessage()

        values = window._attributes.difference_values()                 # the anchor vs itself
        assert values["amplitude"] == 0 and values["pitch"] is None      # ... and clicks have no pitch
        window._table.setCurrentIndex(window._proxy.index(_proxy_row_named(window, "hit.wav"), 0))
        assert window._attributes.difference_values()["amplitude"] > 0
        assert "selected: hit.wav" in window._attributes._diff_caption.text()
        assert window._map.scored                                       # coloured by the ranking

        # A narrowed axis range now filters on the anchor distances (§9.5).
        window._attributes._range_max["conceptual"].setValue(10)
        assert window._proxy.rowCount() == 1                       # hit.wav is far in CLAP space
        window._attributes._range_max["conceptual"].setValue(100)
        assert window._proxy.rowCount() == 2

        # Visible-only scope ranks just what the list shows.
        window._filter.setText("hit")
        window._recompute._rank_visible.setChecked(True)
        _run_ranking(window)
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

        # CLAP's numbers, not a label: four sortable columns, bars, a minimum-score filter.
        window._autoplay.setChecked(False)
        window._table.setCurrentIndex(window._proxy.index(0, 0))
        values = panel.clap_values()
        assert all(values[name] is not None for name in ("rhythmic", "melodic", "vocal", "other"))
        assert sum(values.values()) == pytest.approx(100, abs=3)
        assert window._proxy.data(window._proxy.index(0, 4)).isdigit()
        panel._clap_min["rhythmic"].setValue(100)
        assert window._proxy.rowCount() == 0
        panel._clap_min["rhythmic"].setValue(0)
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
    """Empty index → Add folder (scans it in) → Run with Attributes ticked,
    all from the window, on the worker thread; the list reloads itself after
    each job; the Library panel's flags decide what the list shows, Remove
    folder deletes rows, Rescan walks the folders in scope."""
    from crate.main import MainWindow

    lib = _write_library(tmp_path / "lib")
    settings = _ini(tmp_path)
    window = MainWindow(
        db_path=tmp_path / "index.db", cache_dir=tmp_path / "cache",
        settings=settings, encoder_factory=_encoder,
    )
    try:
        panel = window._recompute
        assert window._proxy.rowCount() == 0 and not panel.running and panel.folder_paths() == []
        assert not panel.run_rescan() and "nothing in scope" in panel.log_text()

        assert not panel.add_folder(tmp_path / "nowhere")          # not a folder: told
        assert panel.add_folder(lib)                                # registered in scope, then scanned
        assert panel.running
        _wait_until(app, lambda: not panel.running)
        assert window._proxy.rowCount() == 2                        # reloaded itself
        assert "added 2" in panel.log_text()
        assert panel.folder_paths() == [normalize(lib)] == panel.scope_folders()
        item = panel._folders.topLevelItem(0)
        assert item.text(3) == "2" and item.checkState(1) == Qt.CheckState.Checked

        panel._step_attributes.setChecked(True)
        panel._step_layout.setChecked(False)
        panel._step_ranking.setChecked(False)
        item.setCheckState(1, Qt.CheckState.Unchecked)              # dormant: hidden, not deleted
        assert window._proxy.rowCount() == 0 and panel.scope_folders() == []
        assert "0 samples in scope of 2 indexed" in window.statusBar().currentMessage()
        panel.run()                                                 # nothing in scope: refused
        assert not panel.running and "nothing in scope" in panel.log_text()
        item.setCheckState(1, Qt.CheckState.Checked)
        assert window._proxy.rowCount() == 2

        panel.run()                                                 # Attributes over the scope
        _wait_until(app, lambda: not panel.running)
        status = window.statusBar().currentMessage()
        assert "· 2 analysed" in status and "· 2 embedded" in status
        assert "[embedding]" in panel.log_text()

        item = panel._folders.topLevelItem(0)                       # rebuilt after the job
        item.setCheckState(0, Qt.CheckState.Checked)                # the root flag
        assert panel.library_root == normalize(lib)
        assert panel.add_folder(lib / "Drums")                      # inside the root: scanned again
        _wait_until(app, lambda: not panel.running)
        assert panel.folder_paths() == [normalize(lib), normalize(lib / "Drums")]
        assert window._proxy.rowCount() == 2                        # the same two files, once each
        assert panel.library_root == normalize(lib)                 # the root flag survived the refresh
        panel.set_in_scope(lib, False)
        assert window._proxy.rowCount() == 1                        # Drums only
        assert "1 samples in scope of 2 indexed" in window.statusBar().currentMessage()

        assert panel.remove_folder(lib / "Drums", confirm=False)    # deletes its rows
        _wait_until(app, lambda: not panel.running)
        assert "1 samples with their analysis" in panel.log_text()
        assert panel.folder_paths() == [normalize(lib)]
        assert window._proxy.rowCount() == 0
        assert "0 samples in scope of 1 indexed" in window.statusBar().currentMessage()
        panel.set_in_scope(lib, True)
        assert window._proxy.rowCount() == 1

        assert panel.run_rescan()                                   # walks the folders in scope
        _wait_until(app, lambda: not panel.running)
        assert "added 1" in panel.log_text()                        # Drums' loop is back under lib
        assert window._proxy.rowCount() == 2

        settings.sync()
        stored = (tmp_path / "crate.ini").read_text(encoding="utf-8")
        assert "step_attributes" in stored                          # the ticked steps persist
    finally:
        window.close()


def test_run_executes_the_ticked_steps_in_order(app, tmp_path):
    """One Run: Attributes, then Map layout, then Ranking — each step's job
    starting the next; Ranking stays unticked-able without an anchor."""
    from crate.main import MainWindow

    lib = _write_library(tmp_path / "lib")
    t = np.arange(SR) / SR                                          # a layout needs three samples
    sf.write(lib / "tone.wav", (0.8 * np.sin(2 * np.pi * 220 * t)).astype("float32"), SR)
    window = MainWindow(
        db_path=tmp_path / "index.db", cache_dir=tmp_path / "cache", settings=_ini(tmp_path),
        encoder_factory=_encoder, reducer_factory=PcaReducer,
    )
    try:
        window._autoplay.setChecked(False)
        panel = window._recompute
        assert panel.add_folder(lib)
        _wait_until(app, lambda: not panel.running)

        panel._step_attributes.setChecked(True)
        panel._step_layout.setChecked(True)
        panel._step_ranking.setChecked(True)
        assert not panel._step_ranking.isEnabled()                  # no anchor: ranking cannot run
        assert panel.plan() == RunPlan(attributes=True, layout="library", ranking=None)
        panel.run()
        _wait_until(app, lambda: not panel.running and "samples placed" in panel.log_text(), timeout_s=180)
        log = panel.log_text()
        assert log.index("— recompute attributes —") < log.index("— recompute map layout —")
        assert window._map.point_count == 3 and not window._plan_steps

        window._table.setCurrentIndex(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))
        window._anchor_current()
        assert panel._step_ranking.isEnabled()
        panel._layout_anchored.setChecked(True)
        assert panel.plan() == RunPlan(attributes=True, layout="anchored", ranking="whole")
        panel.run()
        _wait_until(app, lambda: not panel.running and window._samples.has_similarity, timeout_s=180)
        log = panel.log_text()
        assert log.rindex("— recompute attributes —") < log.rindex("— place the anchor in the map layout —")
        assert "ranked 3 samples" in window.statusBar().currentMessage()
        assert not window._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)
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
    (tmp_path / "a_b").mkdir()
    add_library(panel._conn, tmp_path / "a_b")                    # the scope lives in the index
    panel.refresh_folders()

    collected = panel.collect_settings()
    assert collected.force_full and collected.scope == (normalize(tmp_path / "a_b"),)
    assert collected.segmentation.sensitivity == 0.4 and collected.segmentation.max_segments == 8
    assert collected.one_shot_max_duration_s is None and not collected.embedding.embed_segments

    panel.save_settings()
    settings.sync()
    again = RecomputePanel(tmp_path / "index.db", _ini(tmp_path))
    assert again.collect_settings() == collected

    again._min_length.setValue(5.0)                               # min ≥ max: refused,
    again._max_length.setValue(1.0)                               # nothing starts
    assert not again.run_attributes()
    assert not again.running and "settings:" in again.log_text()

    again.stop()                                                  # no job: a no-op
    assert not again.running
    panel.shutdown()
    again.shutdown()


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


# --- the map view (Phase 6, spec §9.3 / §9.6) ---


def test_map_view_draws_the_layout_and_syncs_with_the_list(app, tmp_path):
    """Layout fit from the Recompute tab (PCA injected), points on the map,
    the list's filter and selection mirrored, badges from a search, halo and
    anchor mark from a ranking, and the anchored-only placement."""
    from crate.main import MainWindow

    lib = _write_library(tmp_path / "lib")
    t = np.arange(SR) / SR
    sf.write(lib / "tone.wav", (0.8 * np.sin(2 * np.pi * 220 * t)).astype("float32"), SR)
    db = tmp_path / "index.db"
    conn = open_db(db)
    scan_library(conn, lib)
    analyze_pending(conn)
    segment_pending(conn)
    embed_pending(conn, encoder=_encoder())
    loop_id = conn.execute("SELECT id FROM samples WHERE filename = 'loop.wav'").fetchone()[0]
    conn.close()

    window = MainWindow(
        db_path=db, cache_dir=tmp_path / "cache", settings=_ini(tmp_path),
        encoder_factory=_encoder, reducer_factory=PcaReducer,
    )
    try:
        window._autoplay.setChecked(False)
        panel = window._recompute
        assert window._map.point_count == 0 and "no layout" in window._map._caption

        panel.set_in_scope(lib, False)                            # the fixture's scan registered lib
        assert not window._run_layout("library")                  # nothing in scope: refused
        assert not panel.running and "nothing in scope" in window.statusBar().currentMessage()
        panel.set_in_scope(lib, True)
        panel._step_attributes.setChecked(False)
        panel._step_layout.setChecked(True)
        panel._step_ranking.setChecked(False)
        panel.run()                                               # Run with Map layout ticked
        _wait_until(app, lambda: not panel.running)
        assert window._map.point_count == 3 and "layout #1" in window._map._caption
        assert "3 samples placed" in panel.log_text()
        assert (tmp_path / "layouts" / "layout_1.pkl").exists()   # next to the segment cache

        window._filter.setText("hit")                             # one shared filtered set
        assert window._map.visible_count == 1
        window._filter.setText("")
        assert window._map.visible_count == 3

        window._map.sample_clicked.emit(loop_id)                  # map → list → preview target
        assert window._current is not None and window._current.name == "loop.wav"
        assert window._map._selected == loop_id

        window._attributes.search_for("kick drum")               # the loop's hit is a segment
        _wait_until(app, lambda: window._search_thread is None)
        assert loop_id in window._map._badges
        window._attributes._clear_search()
        assert loop_id not in window._map._badges

        window._anchor_current()                                  # the loop is current
        _run_ranking(window)
        assert window._map._anchor == loop_id
        assert window._map._halo and loop_id not in window._map._halo
        assert "fit under other weights" not in window._map._caption

        window._run_layout("anchored")                            # cheap transform of one point
        _wait_until(app, lambda: not panel.running)
        assert "anchor placed in layout #1" in panel.log_text()
        assert window._map.point_count == 3

        window._map_button.click()
        assert window._views.currentWidget() is window._map
        assert not window._map.grab().isNull()                    # paints offscreen
        window._list_button.click()
        assert window._views.currentWidget() is window._table
    finally:
        window.close()


# --- §6.4 CLAP windows of long files (2026-09-08) ---


def test_a_search_can_land_on_a_window_inside_a_long_file(app, tmp_path):
    from crate.main import MainWindow

    lib = tmp_path / "lib"
    lib.mkdir()
    sf.write(lib / "ambience.wav", _clicks([1.0, 12.0, 21.0], 25.0), SR)
    sf.write(lib / "hit.wav", _clicks([0.0], 0.4), SR)
    labels = {10.0: "rain on a roof", 5.0: "a door slam", 0.4: "kick drum"}
    db = tmp_path / "index.db"
    conn = open_db(db)
    scan_library(conn, lib)
    analyze_pending(conn)
    segment_pending(conn)
    embed_pending(conn, encoder=FakeEncoder(labels))
    conn.close()

    window = MainWindow(
        db_path=db, cache_dir=tmp_path / "cache", settings=_ini(tmp_path),
        encoder_factory=lambda _settings=None: FakeEncoder(labels),
    )
    try:
        window._autoplay.setChecked(False)
        assert "3 CLAP windows" in window.statusBar().currentMessage()

        window._attributes.search_for("a door slam")
        _wait_until(app, lambda: window._search_thread is None and "door slam" in window.statusBar().currentMessage())
        parent = window._proxy.index(_proxy_row_named(window, "ambience.wav"), 0)
        assert window._proxy.rowCount(parent) == 1
        sub_hit = window._proxy.index(0, 0, parent)
        assert window._proxy.data(sub_hit) == "↳ window @ 20.000 s (5 s)"
        assert window._proxy.data(window._proxy.index(0, 3, parent)) == "window"

        window._table.setCurrentIndex(sub_hit)
        assert window._now_playing.text() == "window @ 20.000 s (5 s) in ambience.wav"
        assert window._current is not None and window._current.name.startswith("seg_") and window._current.exists()
        assert window._current_offset_ms == 20000
        assert window._current_item[0] == "segment"
        assert all(r.detection_method != "window" for r in window._segments._rows)   # not in the drill-down
        assert len(window._waveform._windows) == 3
        assert "3 CLAP windows" in window._waveform._header_text()
        assert window._waveform._selected_segment == window._current_item[1]
        assert window._attributes._strip.dimensions == 32                             # the window's own vector
        assert not window._waveform.grab().isNull()
    finally:
        window.close()
