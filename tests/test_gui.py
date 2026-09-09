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
from crate.listmodel import SECTION_LABEL_ROLE, ColumnFilter, ListProxy, SampleTreeModel, SegmentTableModel
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
        if window._proxy.data(window._proxy.index(r, SampleTreeModel.COL_FILE)) == name
    )


def _anchor_current(app, window) -> None:
    """Anchor the selected row (the A shortcut / a click on its ⚓) and wait for
    the feature table's first load, which happens on a thread."""
    window._anchor_current()
    _wait_until(app, lambda: window._feature_thread is None and not window._feature_waiters and not window._expand_queue)


def _rerank(app, window) -> None:
    """The weight bars re-rank on a short debounce; pump until it has fired."""
    _wait_until(app, lambda: not window._rerank_timer.isActive() and not window._feature_waiters)


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
    files = {model.data(model.index(r, SampleTreeModel.COL_FILE)) for r in range(2)}
    assert files == {"hit.wav", "loop.wav"}
    assert model.headerData(SampleTreeModel.COL_LENGTH, Qt.Orientation.Horizontal) == "Length"
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
    assert proxy.data(proxy.index(0, SampleTreeModel.COL_FILE)) == "loop.wav"        # 4.0 s before 0.4 s

    proxy.setFilterFixedString("drums")                       # matches the Folder column
    assert proxy.rowCount() == 1 and proxy.data(proxy.index(0, SampleTreeModel.COL_FILE)) == "loop.wav"
    proxy.setFilterFixedString("one-shot")                    # matches the Type column
    assert proxy.rowCount() == 1 and proxy.data(proxy.index(0, SampleTreeModel.COL_FILE)) == "hit.wav"
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
    conn.execute(
        "INSERT INTO text_tags (sample_id, tag_or_caption, source_model, created_at) "
        "SELECT id, 'a click loop', 'qwen2audio-caption', 't' FROM samples WHERE filename = 'loop.wav'"
    )
    conn.commit()
    window = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path))
    try:
        assert window._proxy.rowCount() == 2
        assert "2 samples" in window.statusBar().currentMessage()
        assert window._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)
        assert window._table.isColumnHidden(SampleTreeModel.COL_MATCH)

        # Select the loop: its segments appear, the preview target is set.
        window._autoplay.setChecked(False)
        window._table.setCurrentIndex(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))
        assert window._segment_rows
        assert window._current is not None and window._current.name == "loop.wav"
        assert window._tag_bars.tags                                      # the header's tag bars
        assert "a click loop" in window._waveform_panel.caption_text     # the §5.2 sentence, at the top of the waveform
        assert window._header_strip.dimensions == 32                      # the fake model's vector, in the header
        window._waveform.wait_for_load()                                  # read on a thread: the preview never waits
        assert window._waveform.loaded and window._waveform.duration_s == pytest.approx(4.0, abs=0.01)

        window._select_segment(window._segment_rows[0].id)                # a click inside a segment on the waveform
        assert window._current is not None and window._current.name.startswith("seg_")
        assert window._current.exists()
        first = window._segment_rows[0].id
        assert window._waveform._selected_segment == first               # mirrored on the waveform
        assert window._current_offset_ms == window._segment_rows[0].start_ms

        window._on_column_filter(SampleTreeModel.COL_FILE, ColumnFilter(text="hit"))   # the File header's filter
        assert window._proxy.rowCount() == 1 and SampleTreeModel.COL_FILE in window._header.filters
    finally:
        window.close()


# --- Phase 7: search, anchor, ranking, filters (spec §9.4, §9.5, §9.6) ---


def test_text_search_scores_the_list_and_nests_a_sub_hit(app, index, tmp_path):
    from crate.main import MainWindow

    db, conn, cache = index
    window = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path), encoder_factory=_encoder)
    try:
        window._autoplay.setChecked(False)
        window._search_panel.search_for("kick drum")
        assert window._search_thread is not None                # embedding runs off the GUI thread
        _wait_until(app, lambda: window._search_thread is None and window._samples.has_match and not window._expand_queue)

        assert not window._table.isColumnHidden(SampleTreeModel.COL_MATCH)
        assert "kick drum" in window.statusBar().currentMessage()
        top = window._proxy.index(0, 0)
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_MATCH)) == "100"
        # The loop's segments are "kick drum" while the loop itself is a pad:
        # the segment shows as a sub-hit row under its parent (§9.4).
        loop = window._proxy.index(_proxy_row_named(window, "loop.wav"), 0)
        assert window._proxy.rowCount(loop) >= 1 and window._table.isExpanded(loop)   # its sections, the hit first
        sub_hit = window._proxy.index(0, 0, loop)
        assert window._proxy.data(sub_hit, SECTION_LABEL_ROLE).startswith("↳ hit @")   # drawn in the first column shown
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_TYPE, loop)) == "hit"
        assert window._proxy.rowCount(sub_hit) == 0
        del top

        # Selecting the sub-hit previews the rendered segment; dragging it hands out that file.
        window._table.setCurrentIndex(sub_hit)
        assert window._current is not None and window._current.name.startswith("seg_")
        assert window._current_item[0] == "segment"
        assert window._segment_rows                             # the parent's segments, on the waveform
        assert window._tag_bars.tags                              # ... and the tag bars are the parent's
        mime = window._samples.mimeData([window._proxy.mapToSource(sub_hit)])
        assert [Path(u.toLocalFile()) for u in mime.urls()] == [window._current]

        # The parent inherits its best hit's score for sorting.
        assert window._proxy.data(window._proxy.index(_proxy_row_named(window, "loop.wav"), SampleTreeModel.COL_MATCH)) == "100"

        window._search_panel.search_for("kick drum")             # a second query while one is in flight
        window._search_panel.search_for("a synth pad")            # ... is replaced by the newest
        _wait_until(app, lambda: window._search_thread is None and "synth pad" in window.statusBar().currentMessage())
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_FILE)) == "loop.wav"

        window._search_panel._clear_search()
        assert window._table.isColumnHidden(SampleTreeModel.COL_MATCH)
        loop = window._proxy.index(_proxy_row_named(window, "loop.wav"), 0)
        assert window._proxy.rowCount(loop) == len(window._segment_rows) > 0     # the sections stay under it…
        assert not window._table.isExpanded(loop)                                # … folded, nothing scores now
    finally:
        window.close()


def test_anchor_unlocks_ranges_and_ranking_and_persists(app, index, tmp_path):
    from crate.main import MainWindow

    db, conn, cache = index
    settings = _ini(tmp_path)
    window = MainWindow(db_path=db, cache_dir=cache, settings=settings, encoder_factory=_encoder)
    try:
        window._autoplay.setChecked(False)
        assert not window._search_panel._ranges_group.isEnabled()
        assert not window._recompute._layout_anchored.isEnabled()

        for slider in window._search_panel._weight_sliders.values():
            slider.setValue(0)
        window._table.setCurrentIndex(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))
        _anchor_current(app, window)                               # anchors, then ranks at once…

        assert window._anchor_name.endswith("loop.wav")
        assert window._samples.anchor == ("sample", window._rows_by_id and next(
            r.id for r in window._rows_by_id.values() if r.filename == "loop.wav"))
        assert window._search_panel._ranges_group.isEnabled()
        assert window._recompute._layout_anchored.isEnabled()
        assert "loop.wav" in window._recompute._anchor_note.text()
        assert "weight" in window.statusBar().currentMessage()        # … but nothing to blend: told
        assert window._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)
        for slider in window._search_panel._weight_sliders.values():
            slider.setValue(100)

        _rerank(app, window)                                       # the bars re-rank on release
        assert not window._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_FILE)) == "loop.wav"   # the anchor itself first
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_SIMILARITY)) == "100"
        assert "ranked 2 samples" in window.statusBar().currentMessage()

        # ⚓ on another row: anchored and ranked in one click, that row on top;
        # ✕ leaves the ranked list as it is.
        window._anchor_delegate.anchor_clicked.emit(window._proxy.index(_proxy_row_named(window, "hit.wav"), 0))
        assert window._anchor_name.endswith("hit.wav")
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_FILE)) == "hit.wav"
        assert "ranked 2 samples" in window.statusBar().currentMessage()
        window._clear_anchor()
        assert window._anchor is None and not window._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_FILE)) == "hit.wav"
        window._anchor_delegate.anchor_clicked.emit(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_FILE)) == "loop.wav"

        values = window._attributes.difference_values()                 # the anchor vs itself
        assert values["amplitude"] == 0 and values["pitch"] is None      # ... and clicks have no pitch
        window._table.setCurrentIndex(window._proxy.index(_proxy_row_named(window, "hit.wav"), 0))
        assert window._attributes.difference_values()["amplitude"] > 0
        assert "selected: hit.wav" in window._attributes._diff_caption.text()
        assert window._map.scored                                       # coloured by the ranking

        # A narrowed axis range now filters on the anchor distances (§9.5).
        window._search_panel._range_max["conceptual"].setValue(10)
        assert window._proxy.rowCount() == 1                       # hit.wav is far in CLAP space
        window._search_panel._range_max["conceptual"].setValue(100)
        assert window._proxy.rowCount() == 2

        settings.sync()
        stored = (tmp_path / "crate.ini").read_text(encoding="utf-8")
        assert "anchor" in stored and "kind=sample" in stored
    finally:
        window.close()

    # The anchor survives a restart (§9.4) — and the list comes up ranked against it.
    again = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path), encoder_factory=_encoder)
    try:
        _wait_until(app, lambda: again._feature_thread is None and not again._feature_waiters)
        assert again._anchor is not None and again._anchor_name.endswith("loop.wav")
        assert again._search_panel._ranges_group.isEnabled()
        assert not again._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)
        again._clear_anchor()
        assert again._anchor is None and not again._search_panel._ranges_group.isEnabled()
    finally:
        again.close()


def test_attribute_filters_apply_to_the_list(app, index, tmp_path):
    from crate.main import MainWindow

    db, conn, cache = index
    window = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path))
    try:
        panel = window._search_panel
        assert window._proxy.rowCount() == 2

        # Type, length and tempo filter from the list's own header (2026-09-08):
        # a checklist of the values present, and min/max ranges on the raw value.
        window._on_column_filter(SampleTreeModel.COL_TYPE, ColumnFilter(values=frozenset({"loop"})))
        assert window._proxy.rowCount() == 1 and window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_FILE)) == "loop.wav"
        window._on_column_filter(SampleTreeModel.COL_TYPE, None)

        window._on_column_filter(SampleTreeModel.COL_LENGTH, ColumnFilter(low=1.0))
        assert window._proxy.rowCount() == 1
        window._on_column_filter(SampleTreeModel.COL_LENGTH, None)

        window._on_column_filter(SampleTreeModel.COL_BPM, ColumnFilter(low=100.0))   # the loop is 120 BPM; the hit has none
        assert window._proxy.rowCount() == 1 and window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_FILE)) == "loop.wav"
        window._on_column_filter(SampleTreeModel.COL_BPM, ColumnFilter(low=200.0))
        assert window._proxy.rowCount() == 0
        window._on_column_filter(SampleTreeModel.COL_BPM, None)
        assert window._proxy.rowCount() == 2
        assert not panel.criteria().clap_min                            # the Search tab keeps only its own filters

        # CLAP's numbers, not a label: the tag scores as bars on Attributes, the
        # four prompt-set numbers only as a minimum-score filter (2026-09-08).
        window._autoplay.setChecked(False)
        window._table.setCurrentIndex(window._proxy.index(0, 0))
        values = [round(score * 100) for _, score in window._tag_bars.tags]   # the tags' cosines, best first
        assert 0 < len(values) <= 10 and values == sorted(values, reverse=True)
        assert all(0 <= v <= 100 for v in values)
        assert "Rhythmic" not in SampleTreeModel.COLUMNS
        panel._clap_min["rhythmic"].setValue(100)
        assert window._proxy.rowCount() == 0
        panel._clap_min["rhythmic"].setValue(0)
        assert window._proxy.rowCount() == 2

        window._search_panel._weight_sliders["pitch"].setValue(30)
        assert window._search_panel.weights()["pitch"] == pytest.approx(0.3)
    finally:
        window.close()

    reopened = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path))
    try:
        assert reopened._search_panel.weights()["pitch"] == pytest.approx(0.3)   # weights persist (§9.4)
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
        panel._step_captions.setChecked(False)
        panel._layout_anchored.setChecked(True)                     # no anchor: falls back to the re-fit
        assert not panel._layout_anchored.isEnabled()
        assert panel.plan() == RunPlan(attributes=True, layout="library")
        panel.run()
        _wait_until(app, lambda: not panel.running and "samples placed" in panel.log_text(), timeout_s=180)
        log = panel.log_text()
        assert log.index("— recompute attributes —") < log.index("— recompute map layout —")
        assert window._map.point_count == 3 and not window._plan_steps

        window._table.setCurrentIndex(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))
        _anchor_current(app, window)                                # anchored: ranked at once
        assert panel._layout_anchored.isEnabled()
        assert "ranked 3 samples" in window.statusBar().currentMessage()
        assert panel.plan() == RunPlan(attributes=True, layout="anchored")
        panel.run()
        _wait_until(
            app,
            lambda: not panel.running and not window._feature_waiters and window._samples.has_similarity,
            timeout_s=180,
        )
        log = panel.log_text()
        assert log.rindex("— recompute attributes —") < log.rindex("— place the anchor in the map layout —")
        assert not window._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)   # re-ranked after the reload
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
    panel._step_captions.setChecked(True)                           # §5.2 captioning: a step, a batch per Run
    panel._caption_batch.setValue(25)
    (tmp_path / "a_b").mkdir()
    add_library(panel._conn, tmp_path / "a_b")                    # the scope lives in the index
    panel.refresh_folders()

    collected = panel.collect_settings()
    assert collected.force_full and collected.scope == (normalize(tmp_path / "a_b"),)
    assert collected.segmentation.sensitivity == 0.4 and collected.segmentation.max_segments == 8
    assert collected.one_shot_max_duration_s is None and not collected.embedding.embed_segments
    assert panel.plan().captions == 25

    panel.save_settings()
    settings.sync()
    again = RecomputePanel(tmp_path / "index.db", _ini(tmp_path))
    assert again.collect_settings() == collected
    assert again.plan().captions == 25                              # the step and its batch persist

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
        panel.run()                                               # Run with Map layout ticked
        _wait_until(app, lambda: not panel.running)
        assert window._map.point_count == 3 and "layout #1" in window._map._caption
        assert "3 samples placed" in panel.log_text()
        assert (tmp_path / "layouts" / "layout_1.pkl").exists()   # next to the segment cache

        window._on_column_filter(SampleTreeModel.COL_FILE, ColumnFilter(text="hit"))   # one shared filtered set
        assert window._map.visible_count == 1
        window._on_column_filter(SampleTreeModel.COL_FILE, None)
        assert window._map.visible_count == 3

        window._map.sample_clicked.emit(loop_id)                  # map → list → preview target
        assert window._current is not None and window._current.name == "loop.wav"
        assert window._map._selected == loop_id

        window._search_panel.search_for("kick drum")               # the loop's hit is a segment
        _wait_until(app, lambda: window._search_thread is None and window._samples.has_match and not window._expand_queue)
        assert loop_id in window._map._badges
        window._search_panel._clear_search()
        assert loop_id not in window._map._badges

        _anchor_current(app, window)                              # the loop is current: anchored + ranked
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

        window._search_panel.search_for("a door slam")
        _wait_until(app, lambda: window._search_thread is None and "door slam" in window.statusBar().currentMessage()
                    and not window._expand_queue)
        parent = window._proxy.index(_proxy_row_named(window, "ambience.wav"), 0)
        assert window._proxy.rowCount(parent) >= 1 and window._table.isExpanded(parent)
        sub_hit = window._proxy.index(0, 0, parent)                    # the best-matching section first
        assert window._proxy.data(sub_hit, SECTION_LABEL_ROLE) == "↳ window @ 20.000 s (5 s)"
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_TYPE, parent)) == "window"

        window._table.setCurrentIndex(sub_hit)
        assert window._current_label == "window @ 20.000 s (5 s) in ambience.wav"
        assert window._current is not None and window._current.name.startswith("seg_") and window._current.exists()
        assert window._current_offset_ms == 20000
        assert window._current_item[0] == "segment"
        assert all(r.detection_method != "window" for r in window._segment_rows)    # not among the segments
        window._waveform.wait_for_load()
        assert len(window._waveform._windows) == 3
        assert "3 CLAP windows" in window._waveform._header_text()
        assert window._waveform._selected_segment == window._current_item[1]
        assert window._header_strip.dimensions == 32                                  # the window's own vector
        assert not window._waveform.grab().isNull()
    finally:
        window.close()


# --- the panes keep their width (2026-09-08) ---


def test_long_names_do_not_move_the_panes_and_their_position_persists(app, index, tmp_path):
    """A long file name in the transport row used to raise the left pane's
    minimum width and squeeze the right panel — and the splitter kept the
    squeeze. Now the labels elide, and a dragged position survives a restart."""
    from crate.main import MainWindow

    db, conn, cache = index
    settings = _ini(tmp_path)
    window = MainWindow(db_path=db, cache_dir=cache, settings=settings)
    try:
        window.resize(1600, 900)
        window.show()
        app.processEvents()
        before = window._body.sizes()
        assert before[1] >= 360
        assert abs(before[0] - before[1]) <= 8                              # half and half by default (2026-09-08)
        panes = window._panes.sizes()
        assert abs(panes[0] - panes[1]) <= 8
        window._waveform_panel.set_caption("Ambience Los Angeles Street Traffic Cars Pedestrians Dog Night Loop.wav" * 2)
        app.processEvents()
        assert window._body.sizes() == before                                # nothing moved
        label = window._waveform_panel._caption_label
        assert label.minimumSizeHint().width() == 0
        assert label.text().startswith("Ambience") and label.toolTip() == label.text()   # the full text is kept

        # "Dragged" by hand — to a width above the left pane's own minimum (the
        # transport row's buttons; larger under the test's default font).
        left = window._body.widget(0)
        target = max(before[0] + 100, left.minimumSizeHint().width() + 40)   # away from the half-and-half default
        assert target != before[0], (before, left.minimumSizeHint().width())
        window._body.setSizes([target, before[0] + before[1] - target])
        window._panes.setSizes([400, 200])
        app.processEvents()
        dragged, panes = window._body.sizes(), window._panes.sizes()
        assert dragged[0] == target
    finally:
        window.close()

    again = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path))
    try:
        again.resize(1600, 900)
        again.show()
        app.processEvents()
        assert again._body.sizes() == dragged and again._panes.sizes() == panes
    finally:
        again.close()


# --- captioning in parts and one sample at a time (2026-09-08) ---


def test_captions_step_runs_in_batches_and_the_button_does_one_sample(app, index, tmp_path):
    from test_qwen_audio import FakeCaptioner

    from crate.main import MainWindow

    db, conn, cache = index
    fakes: list[FakeCaptioner] = []

    def factory():
        fakes.append(FakeCaptioner())
        return fakes[-1]

    window = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path), captioner_factory=factory)
    try:
        window._autoplay.setChecked(False)
        panel = window._recompute
        assert window.findChild(type(window._search_panel)) is window._search_panel   # the third tab exists
        panel._step_attributes.setChecked(False)
        panel._step_layout.setChecked(False)
        panel._step_captions.setChecked(True)
        panel._caption_batch.setValue(1)
        assert panel.plan() == RunPlan(captions=1)

        panel.run()                                                 # one file this Run…
        _wait_until(app, lambda: not panel.running and "captioned 1 samples" in panel.log_text())
        panel.run()                                                 # …the other the next
        _wait_until(app, lambda: not panel.running and panel.log_text().count("captioned 1 samples") == 2)
        assert len(fakes) == 1 and fakes[0].calls == 2               # one model instance, kept
        assert conn.execute(
            "SELECT COUNT(*) FROM text_tags WHERE source_model = 'qwen2audio-caption'"
        ).fetchone()[0] == 2
        panel.run()                                                 # nothing left: idle
        _wait_until(app, lambda: not panel.running and "captioned 0 samples" in panel.log_text())

        window._table.setCurrentIndex(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))
        assert "a clip of 4.0 s" in window._waveform_panel.caption_text
        assert window._waveform_panel._caption_button.text() == "Recaption"
        conn.execute("UPDATE text_tags SET tag_or_caption = 'stale words' WHERE source_model = 'qwen2audio-caption'")
        conn.commit()
        window._waveform_panel._caption_button.click()              # this sample only, rewritten
        _wait_until(app, lambda: not panel.running and "caption loop.wav" in panel.log_text())
        _wait_until(app, lambda: "a clip of 4.0 s" in window._waveform_panel.caption_text)
        assert fakes[0].calls == 3
        assert window._current is not None and window._current.name == "loop.wav"   # the selection survived the reload
        assert conn.execute(
            "SELECT tag_or_caption FROM text_tags t JOIN samples s ON s.id = t.sample_id "
            "WHERE s.filename = 'hit.wav' AND t.source_model = 'qwen2audio-caption'"
        ).fetchone()[0] == "stale words"                            # the other one untouched
    finally:
        window.close()


# --- ⚓ anchored-only Recompute attributes (2026-09-08, the rest of Phase 8) ---


def test_anchor_only_attributes_redo_the_anchor_alone(app, index, tmp_path):
    """§9.6's fast loop: with an anchor, the Attributes step can run against it
    alone — every stage again under the current settings, nothing else touched
    — and an anchored hit that gets re-detected hands the anchor to its parent."""
    from crate.main import MainWindow

    db, conn, cache = index
    window = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path), encoder_factory=_encoder)
    try:
        window._autoplay.setChecked(False)
        panel = window._recompute
        assert not panel._attributes_anchored.isEnabled()
        panel._attributes_anchored.setChecked(True)                 # no anchor: not a choice
        assert panel.collect_settings().sample_ids == ()

        loop_id = conn.execute("SELECT id FROM samples WHERE filename = 'loop.wav'").fetchone()[0]
        hit_id = conn.execute("SELECT id FROM samples WHERE filename = 'hit.wav'").fetchone()[0]
        seg_id = conn.execute(
            "SELECT id FROM segments WHERE sample_id = ? AND detection_method = 'auto' ORDER BY start_ms",
            (loop_id,),
        ).fetchone()[0]
        auto = lambda sid: conn.execute(  # noqa: E731
            "SELECT COUNT(*) FROM segments WHERE sample_id = ? AND detection_method = 'auto'", (sid,)
        ).fetchone()[0]
        assert auto(loop_id) > 1
        stamps = dict(conn.execute("SELECT sample_id, analyzed_at FROM analysis").fetchall())

        window._anchor_and_rank("segment", seg_id)                  # a hit of loop.wav is the anchor
        _wait_until(app, lambda: window._feature_thread is None and not window._feature_waiters)
        assert window._anchor == ("segment", seg_id)
        assert panel._attributes_anchored.isEnabled() and panel._layout_anchored.isEnabled()
        panel._layout_anchored.setChecked(True)
        panel._attributes_anchored.setChecked(True)
        assert panel._layout_anchored.isChecked()                   # its own group: the layout's choice stays
        panel._max_segments.setValue(1)
        panel._step_attributes.setChecked(True)
        panel._step_layout.setChecked(False)
        panel._step_captions.setChecked(False)
        assert panel.collect_settings().sample_ids == (loop_id,)   # the hit's parent
        assert panel.plan() == RunPlan(attributes=True)

        panel.run()
        _wait_until(
            app,
            lambda: not panel.running and window._feature_thread is None and not window._feature_waiters,
            timeout_s=180,
        )
        log = panel.log_text()
        assert "— recompute attributes (anchor only) —" in log and "loop.wav" in log
        assert auto(loop_id) == 1                                   # the cap applied to the anchor…
        now = dict(conn.execute("SELECT sample_id, analyzed_at FROM analysis").fetchall())
        assert now[loop_id] != stamps[loop_id] and now[hit_id] == stamps[hit_id]   # … and to nothing else
        assert window._anchor == ("sample", loop_id)                # the hit is gone: its parent is the anchor
        assert window._anchor_name.endswith("loop.wav")
        assert "anchored on its parent" in log

        window._clear_anchor()
        assert not panel._attributes_anchored.isEnabled() and panel._changed_only.isChecked()
    finally:
        window.close()


# --- Phase 9 (2026-09-08): Save / Delete segment from the waveform panel ---


def test_save_and_delete_segments_from_the_waveform(app, index, tmp_path):
    """§9.2 / §6.3: staged markers are written only by Save — a moved automatic
    segment becomes manual and confirmed with its render dropped, a drawn one
    is created and described — and Delete removes one after asking."""
    from crate.main import MainWindow

    db, conn, cache = index
    window = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path), encoder_factory=_encoder)
    try:
        window._autoplay.setChecked(False)
        panel, view, jobs = window._waveform_panel, window._waveform, window._recompute
        window._table.setCurrentIndex(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))
        loop_id = conn.execute("SELECT id FROM samples WHERE filename = 'loop.wav'").fetchone()[0]
        first = load_segments(conn, loop_id)[0]
        conn.execute("UPDATE segments SET cache_path = 'old.wav' WHERE id = ?", (first.id,))
        conn.commit()
        assert not panel._save.isEnabled() and not panel._delete.isEnabled()

        view.stage_edit(first.id, first.start_ms + 20, first.end_ms + 40)
        view.add_draft(3000, 3500)
        assert panel._save.text() == "Save 2 segments"
        panel._save.click()
        assert not view.has_staged                                 # handed to the job

        def saved():
            return conn.execute(
                "SELECT id FROM segments WHERE sample_id = ? AND start_ms = 3000 AND end_ms = 3500 "
                "AND detection_method = 'manual' AND is_user_confirmed = 1", (loop_id,),
            ).fetchone()
        _wait_until(app, lambda: not jobs.running and saved() is not None)
        new_id = saved()[0]
        assert "saved 1 new and 1 moved" in jobs.log_text()
        moved = conn.execute(
            "SELECT start_ms, end_ms, detection_method, is_user_confirmed, needs_review, cache_path "
            "FROM segments WHERE id = ?", (first.id,),
        ).fetchone()
        assert tuple(moved) == (first.start_ms + 20, first.end_ms + 40, "manual", 1, 0, None)
        assert conn.execute("SELECT COUNT(*) FROM segment_analysis WHERE segment_id = ?", (new_id,)).fetchone()[0] == 1
        _wait_until(app, lambda: any(s.id == new_id for s in window._segment_rows))   # the reload shows it
        view.wait_for_load()
        assert window._current_sample == loop_id and "2 manual" in view._header_text()

        window._select_segment(new_id)
        assert view.selected_segment == new_id and panel._delete.isEnabled()
        asked: list[str] = []
        window._confirm = lambda question: asked.append(question) is None and False
        panel._delete.click()                                      # refused: nothing happens
        assert len(asked) == 1 and "3.000 s" in asked[0] and not jobs.running
        assert conn.execute("SELECT COUNT(*) FROM segments WHERE id = ?", (new_id,)).fetchone()[0] == 1
        window._confirm = lambda question: True
        panel._delete.click()
        _wait_until(app, lambda: not jobs.running
                    and conn.execute("SELECT COUNT(*) FROM segments WHERE id = ?", (new_id,)).fetchone()[0] == 0)
        _wait_until(app, lambda: all(s.id != new_id for s in window._segment_rows))
        assert "deleted" in jobs.log_text() and window._current_sample == loop_id
        assert window._current_item == ("sample", loop_id)            # the gone segment's parent is current now
        view.wait_for_load()
        assert view.loaded and window._current is not None and window._current.name == "loop.wav"
    finally:
        window.close()


# --- the anchor circle (2026-09-08): a second click on the filled circle clears the anchor ---


def test_a_second_click_on_the_anchor_circle_clears_the_anchor(app, index, tmp_path):
    from crate.main import MainWindow

    db, conn, cache = index
    window = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path), encoder_factory=_encoder)
    try:
        window._autoplay.setChecked(False)
        assert window._waveform_panel.play_button.text() == "▶" and window._autoplay is window._waveform_panel.autoplay
        row = window._proxy.index(_proxy_row_named(window, "loop.wav"), 0)
        window._table.setCurrentIndex(row)
        _anchor_current(app, window)
        assert window._anchor is not None and window._anchor_name.endswith("loop.wav")
        assert not window._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)

        window._on_anchor_clicked(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))   # the filled circle
        assert window._anchor is None and window._anchor_name is None
        assert not window._table.isColumnHidden(SampleTreeModel.COL_SIMILARITY)   # the ranking stays, as with ✕

        window._on_anchor_clicked(window._proxy.index(_proxy_row_named(window, "hit.wav"), 0))    # an empty one anchors
        _wait_until(app, lambda: window._anchor is not None and not window._feature_waiters)
        assert window._anchor_name.endswith("hit.wav")
    finally:
        window.close()


# --- every section under its sample, ordered by similarity when ranked (2026-09-08) ---


def test_every_section_sits_under_its_sample_ordered_by_similarity(app, index, tmp_path):
    from crate.listmodel import SORT_ROLE
    from crate.main import MainWindow

    db, conn, cache = index
    window = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path), encoder_factory=_encoder)
    try:
        window._autoplay.setChecked(False)
        assert list(SampleTreeModel.COLUMNS[:5]) == ["", "", "Folder", "File", "Caption"]
        header = window._table.header()
        assert header.visualIndex(SampleTreeModel.COL_TREE) == 0 and header.visualIndex(SampleTreeModel.COL_ANCHOR) == 1
        assert header.visualIndex(SampleTreeModel.COL_FOLDER) == 2 and header.visualIndex(SampleTreeModel.COL_FILE) == 3
        loop_id = conn.execute("SELECT id FROM samples WHERE filename = 'loop.wav'").fetchone()[0]
        expected = conn.execute(
            "SELECT COUNT(*) FROM segments WHERE sample_id = ? AND detection_method != 'window'", (loop_id,)
        ).fetchone()[0]
        loop = window._proxy.index(_proxy_row_named(window, "loop.wav"), 0)
        n = window._proxy.rowCount(loop)
        assert n == expected > 1 and not window._table.isExpanded(loop)         # all of them, folded
        starts = [window._proxy.data(window._proxy.index(i, SampleTreeModel.COL_FOLDER, loop), SORT_ROLE) for i in range(n)]
        assert starts == sorted(starts)                                          # in time order, nothing scored
        assert window._proxy.data(window._proxy.index(0, SampleTreeModel.COL_CAPTION)) in ("", "a click loop")

        window._table.setCurrentIndex(window._proxy.index(1, 0, loop))           # anchor the second section
        _anchor_current(app, window)
        assert window._anchor is not None and window._anchor[0] == "segment"
        loop = window._proxy.index(_proxy_row_named(window, "loop.wav"), 0)
        assert window._proxy.rowCount(loop) == n and window._table.isExpanded(loop)
        sims = [window._proxy.data(window._proxy.index(i, SampleTreeModel.COL_SIMILARITY, loop), SORT_ROLE) for i in range(n)]
        assert sims == sorted(sims, reverse=True) and sims[0] > sims[-1]         # best first: the anchored one
        assert window._samples.anchor == ("segment", window._samples.hit_at(
            window._proxy.mapToSource(window._proxy.index(0, 0, loop))).segment_id)
    finally:
        window.close()


# --- configurable, remembered columns; the Sections toggle; the window remembers itself (2026-09-09) ---


def test_columns_are_configurable_and_the_window_remembers_itself(app, index, tmp_path):
    from PySide6.QtWidgets import QMenu

    from crate.main import MainWindow

    db, conn, cache = index
    settings = _ini(tmp_path)
    window = MainWindow(db_path=db, cache_dir=cache, settings=settings)
    try:
        window._autoplay.setChecked(False)
        window.resize(1500, 850)
        window.show()
        app.processEvents()
        header = window._header
        C = SampleTreeModel
        assert header.sectionsMovable() and window._table.treePosition() == C.COL_TREE
        assert header.first_visible_column() == C.COL_FOLDER
        for column in C.FIXED:                                           # pinned: no drag, no resize, no popup
            header.moveSection(header.visualIndex(column), 5)
            assert header.visualIndex(column) == column
            header.open_filter(column)
            assert header.popup is None
            assert header.sectionResizeMode(column) == header.ResizeMode.Fixed
        assert header.sections_open                                       # the expander's cell toggled instead
        window._toggle_sections(False)
        header.moveSection(header.visualIndex(C.COL_FILE), 0)             # nothing lands among the pinned ones
        assert header.visualIndex(C.COL_FILE) == 3

        header.moveSection(header.visualIndex(C.COL_FOLDER), 3)          # dragged after File: File is first now
        assert header.first_visible_column() == C.COL_FILE and settings.value("list/header2") is not None
        assert window._anchor_delegate._first() == C.COL_FILE             # the section labels moved with it

        header.set_column_visible(C.COL_BPM, False)                       # hidden…
        assert header.isSectionHidden(C.COL_BPM) and header.hidden_columns() == [C.COL_BPM]
        position = header.visualIndex(C.COL_TAGS)
        header.replace_column(C.COL_TAGS, C.COL_BPM)                      # …then back, in Tags' place
        assert not header.isSectionHidden(C.COL_BPM) and header.visualIndex(C.COL_BPM) == position
        assert header.isSectionHidden(C.COL_TAGS)
        header.set_column_visible(C.COL_SIMILARITY, False)                # the score columns are the view's
        assert C.COL_SIMILARITY not in header.hidden_columns()
        menu = header.column_menu(C.COL_KEY)
        assert isinstance(menu, QMenu)
        texts = [a.text() for a in menu.actions()]
        assert "Reset columns" in texts and any(t.startswith("Replace") for t in texts)
        assert "Key" in texts and "Similarity" not in texts
        assert all(a.text() for a in menu.actions() if a.isCheckable())     # the fixed columns are not offered
        for column in range(C.COL_FOLDER, C.COL_HITS + 1):                # never the last one shown
            header.set_column_visible(column, False)
        assert len([c for c in range(C.COL_FOLDER, C.COL_HITS + 1) if not header.isSectionHidden(c)]) == 1
        assert not any(header.isSectionHidden(c) for c in C.FIXED)
        header.reset_columns()
        assert header.first_visible_column() == C.COL_FOLDER and not header.hidden_columns()
        header.moveSection(header.visualIndex(C.COL_FOLDER), 3)
        header.set_column_visible(C.COL_TAGS, False)
        window._table.sortByColumn(C.COL_LENGTH, Qt.SortOrder.DescendingOrder)

        loop = window._proxy.index(_proxy_row_named(window, "loop.wav"), 0)
        assert not window._table.isExpanded(loop) and not header.sections_open
        header.tree_clicked.emit()                                        # the expander column's header cell
        assert window._table.isExpanded(loop) and header.sections_open
        header.tree_clicked.emit()
        assert not window._table.isExpanded(loop) and not header.sections_open

        window._tabs.setCurrentIndex(2)
        window._map_button.click()
        window.resize(700, 520)                                          # inside the offscreen 800 × 600 screen:
        app.processEvents()                                              # a restored geometry is clamped to it
        window._save_geometry()
    finally:
        window.close()
    settings.sync()

    again = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path))
    try:
        again.show()
        app.processEvents()
        h = again._header
        assert h.first_visible_column() == C.COL_FILE and h.isSectionHidden(C.COL_TAGS)     # as left
        assert h.sortIndicatorSection() == C.COL_LENGTH
        assert again._proxy.data(again._proxy.index(0, C.COL_FILE)) == "loop.wav"        # 4.0 s before 0.4 s: sorted as left
        assert again._tabs.currentIndex() == 2 and again._views.currentWidget() is again._map
        assert abs(again.width() - 700) <= 2 and abs(again.height() - 520) <= 40
    finally:
        again.close()


# --- sorting lives in the model; the selection survives it (2026-09-09) ---


def test_sorting_in_the_model_keeps_the_selection_and_the_open_samples(app, index, tmp_path):
    from crate.main import MainWindow

    db, conn, cache = index
    window = MainWindow(db_path=db, cache_dir=cache, settings=_ini(tmp_path), encoder_factory=_encoder)
    try:
        window._autoplay.setChecked(False)
        C = SampleTreeModel
        assert window._proxy.sortColumn() == -1                            # the proxy never sorts
        window._table.setCurrentIndex(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))
        loop = window._proxy.index(_proxy_row_named(window, "loop.wav"), 0)
        window._table.expand(loop)
        window._table.sortByColumn(C.COL_LENGTH, Qt.SortOrder.AscendingOrder)   # hit (0.4 s) above loop (4.0 s)
        assert window._proxy.data(window._proxy.index(0, C.COL_FILE)) == "hit.wav"
        assert window._proxy.sortColumn() == -1 and window._samples._sort == (C.COL_LENGTH, Qt.SortOrder.AscendingOrder)
        current = window._table.currentIndex()
        assert window._proxy.data(window._proxy.index(current.row(), C.COL_FILE)) == "loop.wav"   # still selected…
        assert window._table.isExpanded(window._proxy.index(_proxy_row_named(window, "loop.wav"), 0))   # … and open
        window._table.sortByColumn(C.COL_LENGTH, Qt.SortOrder.DescendingOrder)
        assert window._proxy.data(window._proxy.index(0, C.COL_FILE)) == "loop.wav"
        assert window._table.isExpanded(window._proxy.index(0, 0))
        window.reload()                                                     # the order survives a reload
        assert window._proxy.data(window._proxy.index(0, C.COL_FILE)) == "loop.wav"
    finally:
        window.close()
