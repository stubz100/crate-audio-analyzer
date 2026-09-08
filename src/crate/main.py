"""The application window — Phase 4.5 "listen and grab", Phase 8's Recompute
tab and Phase 7's list, search and filter (spec §9, §12).

Left: the list — one row per sample, with a sub-hit row underneath where a
segment is the better match (§9.4) — the selected sample's segments, the
preview player and a minimal anchor (§9.2). Right: the Attributes tab
(§9.5) and the Recompute tab (§9.6). The window computes nothing on its
own: ranking and text search run when asked, on features loaded once per
index; a segment renders on first use. The waveform, marker editing and the
rest of the header are Phase 9; the map is Phase 6.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSettings, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableView,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .attributes import AttributesPanel
from .catalog import (
    describe_item,
    hit_label,
    index_summary,
    load_caption,
    load_samples,
    load_segments,
    load_tags,
    load_vector,
    load_windows,
)
from .db import default_db_path, open_db
from .embedding import ClapEncoder, EmbedSettings
from .layout import LayoutSettings, fit_layout, load_current_layout, place_anchor
from .listmodel import AnchorDelegate, ListProxy, SampleTreeModel, SegmentTableModel
from .mapview import MapView
from .library import scope_paths
from .recompute import EncoderFactory, RecomputePanel, RunPlan
from .search import SearchPanel
from .segmentation import create_manual_segment, delete_segment, update_segment
from .render import default_cache_dir, render_segment
from .similarity import AXES, KIND_SAMPLE, KIND_SEGMENT, FeatureTable, Scores
from .theme import ElidedLabel, apply_theme
from .waveform import WaveformPanel

log = logging.getLogger(__name__)

ORG_NAME = "Crate"
APP_NAME = "Crate"
SETTINGS_KEY_AUTOPLAY = "preview/autoplay"
SETTINGS_KEY_ANCHOR_KIND = "anchor/kind"
SETTINGS_KEY_ANCHOR_ID = "anchor/id"
SETTINGS_KEY_SPLITTER = "window/splitter"   # list | right panel, as dragged
SETTINGS_KEY_PANES = "window/panes"         # list | waveform, as dragged
HALO_NEIGHBOURS = 20               # §9.3: nearest neighbours highlighted after a ranking
# The model stack logs every HTTP request at INFO; that is noise on a
# multi-hour run, not progress (same list as the CLI).
_NOISY_LOGGERS = ("httpx", "huggingface_hub", "urllib3", "filelock", "transformers")


class Preview:
    """The bare preview player (spec §9.2, node `I`): play a file, stop.

    Qt Multimedia, so no new dependency; WAV and FLAC decode through the
    platform backend. Unavailable (all methods no-ops) if the multimedia
    module or an output cannot be created — e.g. offscreen in tests.
    """

    def __init__(self, parent) -> None:
        self._player = None
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

            self._output = QAudioOutput(parent)
            self._player = QMediaPlayer(parent)
            self._player.setAudioOutput(self._output)
        except Exception as exc:  # pragma: no cover - platform without multimedia
            log.warning("preview playback unavailable: %s", exc)

    @property
    def available(self) -> bool:
        return self._player is not None

    def play(self, path: Path) -> None:
        if self._player is None:
            return
        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()

    def seek(self, position_ms: int) -> None:
        if self._player is not None:
            self._player.setPosition(int(position_ms))

    @property
    def position_ms(self) -> int | None:
        return None if self._player is None else int(self._player.position())

    @property
    def playing(self) -> bool:
        if self._player is None:
            return False
        from PySide6.QtMultimedia import QMediaPlayer

        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState


class _Outcome:
    """A job's one-line result for the Recompute log (`JobThread` calls `.format()`)."""

    def __init__(self, text: str) -> None:
        self.text = text

    def format(self) -> str:
        return self.text


class _FeatureThread(QThread):
    """Builds the feature table (`similarity.FeatureTable.load`) on its own
    connection, off the GUI thread; carries the reload generation it was
    started under so a stale table is recognised."""

    loaded = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, db_path: Path, generation: int, parent=None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._generation = generation

    def run(self) -> None:  # worker thread
        try:
            conn = open_db(self._db_path)
            try:
                table = FeatureTable.load(conn)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - shown in the status bar
            self.failed.emit(self._generation, f"{type(exc).__name__}: {exc}")
            return
        self.loaded.emit(self._generation, table)


class _EmbedTextThread(QThread):
    """Embeds one search query off the GUI thread. The first call loads
    CLAP — measured at 20 s on a cold start — and even a warm query is a
    model forward pass; neither belongs on the thread that paints."""

    done = Signal(str, object)     # query, vector
    failed = Signal(str, str)      # query, error text

    def __init__(self, encoder, text: str, parent=None) -> None:
        super().__init__(parent)
        self._encoder = encoder
        self._text = text

    def run(self) -> None:  # worker thread
        try:
            vector = self._encoder.embed_text([self._text])[0]
        except Exception as exc:  # noqa: BLE001 - reported, never lost
            self.failed.emit(self._text, f"{type(exc).__name__}: {exc}")
            return
        self.done.emit(self._text, vector)


class MainWindow(QMainWindow):
    """The application's main window.

    `settings` defaults to the per-user store (the registry on Windows);
    tests pass an INI-backed one so they never touch the user's own values.
    `encoder_factory` lets tests run the Recompute tab and text search
    against a fake model; `reducer_factory` does the same for the map layout.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        cache_dir: Path | None = None,
        settings: QSettings | None = None,
        encoder_factory: EncoderFactory | None = None,
        reducer_factory=None,
        captioner_factory=None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Crate")
        self.resize(1400, 800)
        self._settings = settings if settings is not None else QSettings(ORG_NAME, APP_NAME)
        self._db_path = Path(db_path) if db_path is not None else default_db_path()
        self._cache_dir = cache_dir if cache_dir is not None else default_cache_dir()
        self._conn = open_db(self._db_path)
        self._preview = Preview(self)
        self._current_offset_ms = 0                 # a segment preview plays from its start
        self._playhead = QTimer(self)
        self._playhead.setInterval(50)
        self._playhead.timeout.connect(self._on_playhead_tick)
        self._playhead.start()
        self._encoder_factory = encoder_factory
        self._reducer_factory = reducer_factory   # tests inject PCA; None = UMAP (or PCA fallback)
        self._encoder = None
        self._search_thread: _EmbedTextThread | None = None
        self._pending_search: str | None = None
        self._features: FeatureTable | None = None
        self._current: Path | None = None
        self._current_item: tuple[str, int] | None = None
        self._current_sample: int | None = None     # the selected sample (a hit's parent): where markers are saved
        self._anchor: tuple[str, int] | None = None
        self._anchor_parent: int | None = None    # the anchor's sample (a hit's parent)
        self._anchor_vector = None
        self._axis = None
        self._axis_by_sample: dict[int, dict[str, float]] = {}
        self._close_pending = False
        self._quiet_select = False                    # a reload re-selects without replaying
        self._plan_steps: list[tuple[str, str | None]] = []   # the Recompute tab's Run, step by step
        self._feature_thread: _FeatureThread | None = None
        self._feature_generation = 0                            # bumped by reload(): a table loaded before is stale
        self._feature_waiters: list = []                        # callbacks for when the table lands
        self._layout_dir = Path(self._cache_dir).parent / "layouts"
        self._rows_by_id: dict[int, object] = {}
        self._source_row_of: dict[int, int] = {}

        # --- view switch (§9.2) + quick filter ---
        self._list_button = QPushButton("List")
        self._map_button = QPushButton("Map")
        for button in (self._list_button, self._map_button):
            button.setCheckable(True)
            button.setAutoExclusive(True)
        self._list_button.setChecked(True)
        self._last_similarity: Scores | None = None
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Quick filter (file, folder, type, tags…)")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._on_filter_changed)

        # --- the list (samples, with sub-hit rows: §6.4, §9.4) ---
        self._samples = SampleTreeModel(self._render)
        self._proxy = ListProxy(self)
        self._proxy.setSourceModel(self._samples)
        self._table = QTreeView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.setUniformRowHeights(True)
        self._table.setRootIsDecorated(True)
        self._table.setExpandsOnDoubleClick(False)
        self._table.setMouseTracking(True)              # the ⚓ brightens under the mouse
        self._anchor_delegate = AnchorDelegate(self._table)
        self._anchor_delegate.anchor_clicked.connect(self._on_anchor_clicked)
        self._table.setItemDelegateForColumn(0, self._anchor_delegate)
        header = self._table.header()
        header.setResizeContentsPrecision(200)   # measure a sample of rows, not all of them
        header.moveSection(header.visualIndex(SampleTreeModel.COL_MATCH), 1)
        header.moveSection(header.visualIndex(SampleTreeModel.COL_SIMILARITY), 1)
        self._configure_drag_view(self._table)
        self._table.selectionModel().currentRowChanged.connect(self._on_sample_selected)
        self._table.doubleClicked.connect(lambda _index: self._play_current())

        # --- the selected sample's segments (§6.4 drill-down) ---
        self._segments = SegmentTableModel(self._render)
        self._segment_table = QTableView()
        self._segment_table.setModel(self._segments)
        self._configure_drag_view(self._segment_table)
        self._segment_table.verticalHeader().setVisible(False)
        self._segment_table.selectionModel().currentRowChanged.connect(self._on_segment_selected)
        self._segment_table.doubleClicked.connect(lambda _index: self._play_current())

        # --- the map (§9.3): the same filtered set, one point per sample ---
        self._map = MapView()
        self._map.sample_clicked.connect(self._select_sample)
        self._map.sample_activated.connect(self._play_sample)
        self._views = QStackedWidget()
        self._views.addWidget(self._table)
        self._views.addWidget(self._map)
        self._list_button.clicked.connect(lambda: self._views.setCurrentWidget(self._table))
        self._map_button.clicked.connect(lambda: self._views.setCurrentWidget(self._map))

        # --- the waveform panel (§9.2's preview strip, at the bottom on the user's steer;
        # its markers editable since 2026-09-08, Phase 9) ---
        self._waveform_panel = WaveformPanel()
        self._waveform = self._waveform_panel.view
        self._waveform.segment_clicked.connect(self._select_segment_row)
        self._waveform.position_clicked.connect(self._seek)
        self._waveform_panel.save_requested.connect(self._save_segments)
        self._waveform_panel.delete_requested.connect(self._delete_segment)

        tables = QSplitter(Qt.Orientation.Vertical)
        tables.addWidget(self._views)
        tables.addWidget(self._waveform_panel)
        tables.setStretchFactor(0, 4)
        tables.setStretchFactor(1, 1)
        self._panes = tables

        # --- transport + anchor (§9.2, the minimal slice Phase 7 needs) ---
        self._play_button = QPushButton("▶ Play")
        self._play_button.setObjectName("play")
        self._play_button.clicked.connect(self._play_current)
        stop_button = QPushButton("■ Stop")
        stop_button.clicked.connect(self._preview.stop)
        self._autoplay = QCheckBox("Auto-play on select")
        self._autoplay.setChecked(self._settings.value(SETTINGS_KEY_AUTOPLAY, True, type=bool))
        self._autoplay.toggled.connect(
            lambda on: self._settings.setValue(SETTINGS_KEY_AUTOPLAY, bool(on))
        )
        self._now_playing = ElidedLabel("")
        self._now_playing.setObjectName("nowPlaying")
        self._anchor_label = ElidedLabel("no anchor")
        self._anchor_label.setToolTip(
            "The comparison reference (§9.2): press ⚓ at the start of a row to anchor that "
            "sample and rank the list against it (or press A on the selected row)."
        )
        clear_anchor = QPushButton("✕")
        clear_anchor.setToolTip("Clear the anchor — the list keeps its order")
        clear_anchor.setFixedWidth(28)
        clear_anchor.clicked.connect(self._clear_anchor)
        transport = QHBoxLayout()
        transport.addWidget(self._play_button)
        transport.addWidget(stop_button)
        transport.addWidget(self._autoplay)
        transport.addWidget(self._now_playing, stretch=2)
        transport.addWidget(QLabel("⚓"))
        transport.addWidget(self._anchor_label, stretch=1)
        transport.addWidget(clear_anchor)
        transport.addWidget(QLabel("Drag a row into Bitwig ↗"))
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self._toggle_play)
        QShortcut(QKeySequence(Qt.Key.Key_A), self, activated=self._anchor_current)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        top.addWidget(self._list_button)
        top.addWidget(self._map_button)
        top.addWidget(self._filter, stretch=1)
        left_layout.addLayout(top)
        left_layout.addWidget(tables, stretch=1)
        left_layout.addLayout(transport)

        # --- right panel: Attributes (§9.5) and Recompute (§9.6) ---
        self._attributes = AttributesPanel(self._settings)
        self._attributes.host_segments(self._segment_table)
        self._search_panel = SearchPanel(self._settings)
        self._search_panel.search_requested.connect(self._search)
        self._search_panel.search_cleared.connect(self._clear_search)
        self._search_panel.criteria_changed.connect(self._proxy.set_criteria)
        self._attributes.search_requested.connect(self._search_panel.search_for)   # a chip → the Search tab's box
        self._waveform_panel.caption_requested.connect(self._caption_current)   # the caption line's button
        self._recompute = RecomputePanel(
            self._db_path, self._settings, encoder_factory, captioner_factory, parent=self
        )
        self._recompute.index_changed.connect(self.reload)
        self._recompute.index_changed.connect(self._close_if_pending)
        self._recompute.scope_changed.connect(self.reload)
        self._recompute.run_requested.connect(self._run_plan)
        self._recompute.job_ended.connect(self._advance_plan)
        self._search_panel.criteria_changed.connect(self._sync_map_visibility)
        # The weight bars re-rank the anchored list on release (2026-09-08, the
        # user's steer, after ranking measured at ~25 ms): a short debounce so a
        # drag ranks once, at its end, not per pixel.
        self._rerank_timer = QTimer(self)
        self._rerank_timer.setSingleShot(True)
        self._rerank_timer.setInterval(150)
        self._rerank_timer.timeout.connect(self._rerank_for_weights)
        self._attributes.weights_changed.connect(self._on_weights_changed)
        tabs = QTabWidget()
        tabs.addTab(self._attributes, "Attributes")
        tabs.addTab(self._search_panel, "Search")
        tabs.addTab(self._recompute, "Recompute")
        tabs.setMinimumWidth(360)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.addWidget(left)
        body.addWidget(tabs)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 1)
        body.setSizes([840, 560])
        self._body = body
        # The panes keep the size they were dragged to (2026-09-08, the user's
        # steer): the splitter states persist across restarts.
        for splitter, key in ((body, SETTINGS_KEY_SPLITTER), (tables, SETTINGS_KEY_PANES)):
            state = self._settings.value(key, None)
            if state:
                splitter.restoreState(state)
        self.setCentralWidget(body)

        self.reload()
        self._restore_anchor()

    # --- data ---

    def reload(self) -> None:
        """Re-read the index — after a Recompute job, a CLI run outside, or a
        change of scope. The list, the map and the counts show the folders in
        scope (§9.6); dormant folders' rows stay in the index unseen. Scores
        are view state and start over; an anchor keeps its place and its
        distances are recomputed on the fresh features."""
        scope = scope_paths(self._conn)
        keep = self._current_item                        # re-selected below, quietly
        rows = load_samples(self._conn, scope=scope)
        self._rows_by_id = {r.id: r for r in rows}
        self._source_row_of = {r.id: i for i, r in enumerate(rows)}
        self._samples.set_rows(rows)
        self._samples.set_similarity(None)
        self._samples.set_match(None)
        self._segments.set_rows([])
        self._features = None
        self._feature_generation += 1
        self._axis = None                                # the anchor's distances are redone below
        self._axis_by_sample = {}
        self._proxy.set_axis_lookup(lambda _sample_id: None)
        self._update_score_columns()
        self._load_map()
        for column in range(len(SampleTreeModel.COLUMNS)):
            self._table.resizeColumnToContents(column)
        counts = index_summary(self._conn, scope)
        in_scope = (
            f"{counts['samples']} samples in scope of {counts['indexed']} indexed"
            if counts['samples'] != counts['indexed'] else f"{counts['samples']} samples"
        )
        self.statusBar().showMessage(
            f"{in_scope} · {counts['analysed']} analysed · "
            f"{counts['segments']} segments · {counts['windows']} CLAP windows · "
            f"{counts['embedded']} embedded · index: {self._db_path}"
        )
        if keep is not None:
            kind, item_id = keep
            sample_id = item_id if kind == KIND_SAMPLE else self._parent_of_segment(item_id)
            if sample_id in self._rows_by_id:
                self._quiet_select = True
                try:
                    self._select_sample(sample_id)
                finally:
                    self._quiet_select = False
        if self._anchor is not None:
            kind, item_id = self._anchor
            if (
                kind == KIND_SEGMENT and self._parent_of_segment(item_id) is None
                and self._anchor_parent in self._rows_by_id
            ):
                # The anchored hit was an automatic segment the recompute replaced
                # (re-detection writes new rows, §6.2): its parent takes the anchor.
                self._anchor = (KIND_SAMPLE, self._anchor_parent)
                self._recompute.note("the anchored hit was redone: anchored on its parent instead")
            self._anchor_and_rank(*self._anchor)         # an anchored list is a ranked list (§9.2)

    def _ensure_features(self) -> FeatureTable:
        if self._features is None:
            self._features = FeatureTable.load(self._conn)
        return self._features

    def _with_features(self, callback) -> None:
        """Run `callback(features)` once the feature table is loaded — at once
        when it is, else after a worker thread has built it (0.6 s for 4.5k
        samples, seconds at library scale: never on the GUI thread). Every
        caller waits its turn; a reload in the meantime discards the table
        being built and starts over."""
        if self._features is not None:
            callback(self._features)
            return
        self._feature_waiters.append(callback)
        if self._feature_thread is None:
            self._start_feature_load()

    def _start_feature_load(self) -> None:
        self.statusBar().showMessage("loading the feature table (once after each recompute)…")
        thread = _FeatureThread(self._db_path, self._feature_generation, self)
        thread.loaded.connect(self._on_features_loaded)
        thread.failed.connect(self._on_features_failed)
        thread.finished.connect(thread.deleteLater)
        self._feature_thread = thread
        thread.start()

    def _on_features_loaded(self, generation: int, table) -> None:
        self._feature_thread = None
        if generation != self._feature_generation:      # stale: built before a reload
            if self._feature_waiters:
                self._start_feature_load()
            return
        self._features = table
        waiters, self._feature_waiters = self._feature_waiters, []
        for callback in waiters:
            callback(table)

    def _on_features_failed(self, generation: int, error: str) -> None:
        self._feature_thread = None
        self._feature_waiters = []
        self.statusBar().showMessage(f"feature table unavailable: {error}")

    def _render(self, segment_id: int) -> Path:
        return render_segment(self._conn, segment_id, self._cache_dir)

    @staticmethod
    def _configure_drag_view(view: QAbstractItemView) -> None:
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        view.setDragEnabled(True)
        view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        view.setDefaultDropAction(Qt.DropAction.CopyAction)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setAlternatingRowColors(True)

    def _update_score_columns(self) -> None:
        for column, present in (
            (SampleTreeModel.COL_SIMILARITY, self._samples.has_similarity),
            (SampleTreeModel.COL_MATCH, self._samples.has_match),
        ):
            self._table.setColumnHidden(column, not present)
            if present:
                self._table.resizeColumnToContents(column)   # measured while hidden = too narrow

    # --- selection / preview ---

    def _on_sample_selected(self, current, _previous) -> None:
        if not current.isValid():
            return
        source = self._proxy.mapToSource(current)
        row = self._samples.row_at(source)
        hit = self._samples.hit_at(source)
        self._current_sample = row.id
        self._map.set_selected(row.id)
        # The drill-down and the chips are the sample's either way — a sub-hit
        # is a segment *of* that sample (2026-09-07 review).
        segments = load_segments(self._conn, row.id)
        self._segments.set_rows(segments)
        self._segment_table.resizeColumnsToContents()
        self._attributes.show_tags(load_tags(self._conn, row.id))
        self._waveform_panel.set_caption(load_caption(self._conn, row.id))
        attack_ms, decay_ms = self._envelope_marks(row.id)
        # The CLAP windows of a long file (§6.4) are drawn on the waveform as a
        # strip, not listed with the segments: a hit can land on one.
        self._waveform.load(
            Path(row.filepath), row.filename, segments, attack_ms, decay_ms,
            windows=load_windows(self._conn, row.id),
        )
        self._waveform.set_selected_segment(hit.segment_id if hit is not None else None)
        self._current_offset_ms = hit.start_ms if hit is not None else 0
        if hit is not None:
            try:
                self._current = self._render(hit.segment_id)
            except (ValueError, LookupError, OSError) as exc:
                self.statusBar().showMessage(f"hit cannot be rendered: {exc}")
                return
            self._current_item = (KIND_SEGMENT, hit.segment_id)
            self._now_playing.setText(
                f"{hit_label(hit.start_ms, hit.end_ms, hit.window)} in {row.filename}"
            )
        else:
            self._current = Path(row.filepath)
            self._current_item = (KIND_SAMPLE, row.id)
            self._now_playing.setText(row.filename)
        self._update_difference()
        self._show_vector()
        if self._autoplay.isChecked() and not self._quiet_select:
            self._play_current()

    def _on_segment_selected(self, current, _previous) -> None:
        if not current.isValid():
            return
        seg = self._segments.row_at(current)
        try:
            self._current = self._render(seg.id)
        except (ValueError, LookupError, OSError) as exc:
            self.statusBar().showMessage(f"segment cannot be rendered: {exc}")
            return
        self._current_item = (KIND_SEGMENT, seg.id)
        self._current_offset_ms = seg.start_ms
        self._now_playing.setText(hit_label(seg.start_ms, seg.end_ms))
        self._waveform.set_selected_segment(seg.id)
        self._update_difference()
        self._show_vector()
        if self._autoplay.isChecked():
            self._play_current()

    def _play_current(self) -> None:
        if self._current is not None:
            self._preview.play(self._current)

    def _toggle_play(self) -> None:
        if self._preview.playing:
            self._preview.stop()
        else:
            self._play_current()

    def _on_filter_changed(self, text: str) -> None:
        self._proxy.setFilterFixedString(text)
        self._sync_map_visibility()

    # --- the waveform panel and the difference readout (2026-09-07 steer) ---

    def _envelope_marks(self, sample_id: int) -> tuple[float | None, float | None]:
        row = self._conn.execute(
            "SELECT attack_ms, decay_ms FROM analysis WHERE sample_id = ?", (sample_id,)
        ).fetchone()
        return (None, None) if row is None else (row[0], row[1])

    def _update_difference(self) -> None:
        """The selected item's per-axis distance from the anchor (§9.5)."""
        anchor_label = None if self._anchor is None else self._anchor_label.text().lstrip("⚓ ")
        if self._anchor is None or self._axis is None or self._current_item is None:
            self._attributes.show_difference(anchor_label, None, None)
            return
        features = self._ensure_features()
        kind, item_id = self._current_item
        row = features.row_of(kind, item_id)
        if row is None:
            self._attributes.show_difference(anchor_label, self._now_playing.text(), None)
            return
        distances = {axis: float(self._axis[row, j]) for j, axis in enumerate(AXES)}
        self._attributes.show_difference(anchor_label, self._now_playing.text(), distances)

    def _show_vector(self) -> None:
        """The selected item's CLAP vector as stripes, the anchor's beneath it."""
        if self._current_item is None:
            self._attributes.show_vector(None, None)
            return
        kind, item_id = self._current_item
        self._attributes.show_vector(load_vector(self._conn, kind, item_id), self._anchor_vector)

    def _select_segment_row(self, segment_id: int) -> None:
        """A click inside a segment on the waveform selects it in the table."""
        row = self._segments.index_of(segment_id)
        if row is not None:
            self._segment_table.selectRow(row)

    def _seek(self, position_ms: int) -> None:
        """A click on the waveform outside any segment seeks the sample."""
        if self._current_item is None or self._current_item[0] != KIND_SAMPLE:
            return
        self._preview.seek(position_ms)
        if not self._preview.playing:
            self._play_current()
            self._preview.seek(position_ms)

    def _on_playhead_tick(self) -> None:
        if not self._preview.playing:
            self._waveform.set_position_ms(None)
            return
        position = self._preview.position_ms
        self._waveform.set_position_ms(None if position is None else position + self._current_offset_ms)

    # --- manual markers (§9.2, §6.3) ---

    def _save_segments(self) -> None:
        """*Save segment*: the markers moved or drawn on the waveform become
        manual segments (§6.3) — a moved automatic one turns manual and
        confirmed, its cached render and vector dropped. A job, since a
        segment's descriptors decode the parent: it shares the log and the
        reload that shows the result."""
        staged = self._waveform.staged()
        sample_id = self._current_sample
        if not staged or sample_id is None:
            return
        row = self._rows_by_id.get(sample_id)
        name = row.filename if row is not None else f"sample {sample_id}"

        def job(conn, _stop) -> _Outcome:
            created = moved = 0
            for segment_id, start_ms, end_ms in staged:
                if segment_id is None:
                    create_manual_segment(conn, sample_id, start_ms, end_ms)
                    created += 1
                else:
                    update_segment(conn, segment_id, start_ms, end_ms)
                    moved += 1
            return _Outcome(
                f"saved {created} new and {moved} moved segment(s) of {name}: manual now, "
                "exempt from the automatic rules and never overwritten (§6.3)"
            )

        if self._recompute.start_job(f"save {len(staged)} segment(s) of {name}", job):
            self._waveform.discard()                    # written by the job; the reload shows them
        else:
            self.statusBar().showMessage("a job is running: the markers stay unsaved until it ends")

    def _delete_segment(self, segment_id: int) -> None:
        """*Delete segment*: automatic or manual, deliberately — asked first.
        §6.3's protection is against silent automatic overwrite, not against
        this."""
        label = describe_item(self._conn, KIND_SEGMENT, segment_id) or f"segment {segment_id}"
        if not self._confirm(
            f"Delete {label}?\n\nIts descriptors, vector and cached render go with it; "
            "a recompute does not bring a manual segment back."
        ):
            return
        if not self._recompute.start_job(
            f"delete {label}",
            lambda conn, _stop: _Outcome("deleted" if delete_segment(conn, segment_id) else "already gone"),
        ):
            self.statusBar().showMessage("a job is running: try again when it ends")

    def _confirm(self, question: str) -> bool:
        answer = QMessageBox.question(
            self, "Crate", question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # --- anchor (§9.2) and ranking (§9.6) ---

    def _caption_current(self) -> None:
        """The waveform panel's button: caption the selected sample (a hit's
        parent) as a job; the reload afterwards shows the sentence."""
        if self._current_item is None:
            self.statusBar().showMessage("select a sample first")
            return
        kind, item_id = self._current_item
        sample_id = item_id if kind == KIND_SAMPLE else self._parent_of_segment(item_id)
        if sample_id is None:
            return
        row = self._rows_by_id.get(sample_id)
        self._recompute.caption_sample(sample_id, row.filename if row else "")

    def _on_weights_changed(self, _weights) -> None:
        if self._anchor is not None:
            self._rerank_timer.start()

    def _rerank_for_weights(self) -> None:
        if self._anchor is not None:
            self._with_features(lambda _table: self._rank())

    def _anchor_current(self) -> None:
        if self._current_item is None:
            self.statusBar().showMessage("select a sample or a hit first, then anchor it")
            return
        self._anchor_and_rank(*self._current_item)

    def _on_anchor_clicked(self, index) -> None:
        """The ⚓ at the start of a row (§9.2): that sample — or that hit — becomes
        the anchor and the list is ranked against it, at once."""
        source = self._proxy.mapToSource(index)
        hit = self._samples.hit_at(source)
        if hit is not None:
            self._anchor_and_rank(KIND_SEGMENT, hit.segment_id)
        else:
            self._anchor_and_rank(KIND_SAMPLE, self._samples.row_at(source).id)

    def _anchor_and_rank(self, kind: str, item_id: int) -> None:
        """Anchor, then rank the scope against the anchor with the weight bars
        as they are — one pass over the feature table (measured 2026-09-08:
        25 ms for 4.5k samples + 29k segments), so nothing needs to be
        incremental; only the table's first load waits on a thread. A newer
        click while that load runs simply wins."""
        self._feature_waiters = [w for w in self._feature_waiters if getattr(w, "anchor", False) is False]

        def go(features) -> None:
            if self._apply_anchor(kind, item_id, announce=False):
                self._rank()
                self._table.scrollToTop()
            elif self._anchor == (kind, item_id):
                self._clear_anchor()

        go.anchor = True
        self._with_features(go)

    def _apply_anchor(self, kind: str, item_id: int, announce: bool = True) -> bool:
        """Pin `item` and compute every sample's and segment's per-axis
        distance from it once (§9.5). False if it has no analysis row."""
        features = self._ensure_features()
        row = features.row_of(kind, item_id)
        if row is None:
            self.statusBar().showMessage(
                "that item has no analysis yet — run Recompute attributes first, then anchor"
            )
            return False
        label = describe_item(self._conn, kind, item_id) or f"{kind} {item_id}"
        self._anchor = (kind, item_id)
        self._axis = features.distances(row)
        self._axis_by_sample = features.axis_distances_by_sample(self._axis)
        self._settings.setValue(SETTINGS_KEY_ANCHOR_KIND, kind)
        self._settings.setValue(SETTINGS_KEY_ANCHOR_ID, int(item_id))
        self._anchor_label.setText(label)
        self._samples.set_anchor((kind, item_id))
        self._proxy.set_axis_lookup(self._axis_by_sample.get)
        self._anchor_parent = self._anchor_sample_id()
        self._search_panel.set_anchor_state(True)
        self._recompute.set_anchor_available(True, label, self._anchor_parent)
        self._map.set_anchor(self._anchor_parent)
        self._anchor_vector = load_vector(self._conn, kind, item_id)
        self._update_difference()
        self._show_vector()
        if announce:
            self.statusBar().showMessage(f"anchored on {label}")
        return True

    def _clear_anchor(self) -> None:
        self._anchor = None
        self._anchor_parent = None
        self._axis = None
        self._axis_by_sample = {}
        self._settings.remove(SETTINGS_KEY_ANCHOR_KIND)
        self._settings.remove(SETTINGS_KEY_ANCHOR_ID)
        self._anchor_label.setText("no anchor")
        self._samples.set_anchor(None)
        self._proxy.set_axis_lookup(lambda _sample_id: None)
        self._search_panel.set_anchor_state(False)
        self._recompute.set_anchor_available(False)
        # The list keeps its order, its Similarity column and the map its halo
        # (2026-09-08, the user's steer): un-anchoring changes nothing but the anchor.
        self._map.set_anchor(None)
        self._anchor_vector = None
        self._update_difference()
        self._show_vector()
        self._last_similarity = None
        if not self._samples.has_match:
            self._map.set_scores(None)

    def _restore_anchor(self) -> None:
        kind = self._settings.value(SETTINGS_KEY_ANCHOR_KIND, "", type=str)
        item_id = self._settings.value(SETTINGS_KEY_ANCHOR_ID, 0, type=int)
        if kind and item_id:
            self._anchor = (kind, item_id)
            self._anchor_and_rank(kind, item_id)     # the table loads on a thread; the list ranks when it lands

    def _rank(self) -> None:
        """Rank the scope against the anchor: blend its per-axis distances with
        the weight bars (§5.1) and fold the sub-hits (§9.4). Runs on a ⚓
        click, on a weight bar's release, and after every reload."""
        if self._anchor is None or self._axis is None:
            self.statusBar().showMessage("ranking needs an anchor (⚓)")
            return
        weights = self._attributes.weights()
        if not any(weights.values()):
            self.statusBar().showMessage(
                "ranking needs at least one weight above zero (Attributes tab)"
            )
            return
        features = self._ensure_features()
        sample_ids = set(self._rows_by_id)
        scores = features.rank(self._axis, weights, sample_ids)
        self._samples.set_similarity(scores)
        self._update_score_columns()
        self._table.sortByColumn(SampleTreeModel.COL_SIMILARITY, Qt.SortOrder.DescendingOrder)
        self._table.expandAll()
        anchor_sample = self._anchor_sample_id()
        ranked = [
            sid for sid in sorted(scores.sample, key=scores.sample.get, reverse=True)
            if sid != anchor_sample
        ]
        self._map.set_halo(set(ranked[:HALO_NEIGHBOURS]))
        self._update_badges()
        self._last_similarity = scores
        if not self._samples.has_match:
            self._map.set_scores(scores.sample, "similarity to the anchor")
        self.statusBar().showMessage(
            f"ranked {len(scores.sample)} samples against ⚓ {self._anchor_label.text()} "
            f"({len(scores.hits)} hits inside longer samples)"
        )

    # --- free-text search (§5.2) ---

    def _ensure_encoder(self):
        if self._encoder is None:
            self._encoder = (
                self._encoder_factory(EmbedSettings()) if self._encoder_factory else ClapEncoder()
            )
        return self._encoder

    def _search(self, text: str) -> None:
        """Embed the query on a worker thread, then score on this one. A
        query typed while one is in flight replaces it once that lands."""
        if self._search_thread is not None:
            self._pending_search = text
            return
        first_use = " (loading CLAP on first use…)" if self._encoder is None else ""
        self.statusBar().showMessage(f"searching “{text}”{first_use}")
        thread = _EmbedTextThread(self._ensure_encoder(), text, self)
        thread.done.connect(self._apply_search)
        thread.failed.connect(self._search_failed)
        thread.finished.connect(self._search_finished)
        self._search_thread = thread
        thread.start()

    def _search_failed(self, text: str, error: str) -> None:
        self.statusBar().showMessage(f"search unavailable: {error}")

    def _search_finished(self) -> None:
        thread, self._search_thread = self._search_thread, None
        if thread is not None:
            thread.deleteLater()
        pending, self._pending_search = self._pending_search, None
        if pending:
            self._search(pending)

    def _apply_search(self, text: str, query) -> None:
        self._with_features(lambda features: self._score_search(text, query, features))

    def _score_search(self, text: str, query, features: FeatureTable) -> None:
        scores = features.search(query, set(self._rows_by_id))   # the scope (§9.6)
        self._samples.set_match(scores)
        self._update_score_columns()
        self._table.sortByColumn(SampleTreeModel.COL_MATCH, Qt.SortOrder.DescendingOrder)
        self._table.expandAll()
        self._update_badges()
        self._map.set_scores(scores.sample, f"match to “{text}”")
        self.statusBar().showMessage(
            f"“{text}”: {len(scores.sample)} samples scored, "
            f"{len(scores.hits)} hits inside longer samples"
        )

    def _clear_search(self) -> None:
        self._samples.set_match(None)
        self._update_score_columns()
        self._update_badges()
        similarity = self._last_similarity
        self._map.set_scores(similarity.sample if similarity else None, "similarity to the anchor")
        if self._samples.has_similarity:
            self._table.sortByColumn(SampleTreeModel.COL_SIMILARITY, Qt.SortOrder.DescendingOrder)
            self._table.expandAll()

    # --- the map (§9.3) and its layout (§9.6) ---

    def _load_map(self) -> None:
        """Draw the last explicitly computed layout — samples only (§6.4)."""
        info, positions = load_current_layout(self._conn)
        ids = [sid for sid in positions if sid in self._rows_by_id]
        xy = np.array([positions[sid] for sid in ids], dtype=float).reshape(-1, 2)
        rows = [self._rows_by_id[sid] for sid in ids]
        self._map.set_points(ids, xy, [r.filename for r in rows], [r.structural_type or "" for r in rows])
        self._last_similarity = None
        self._map.set_scores(None)
        if info is None:
            caption = "no layout yet — Recompute tab: tick Map layout and press Run"
        else:
            caption = (
                f"layout #{info.id}: {len(ids)} samples · {info.scope_description} · "
                f"{info.reducer} · {info.computed_at[:16].replace('T', ' ')}"
            )
            bars = {k: round(v, 2) for k, v in self._attributes.weights().items()}
            if {k: round(v, 2) for k, v in info.weights.items()} != bars:
                caption += " · fit under other weights than the bars show"
        self._map.set_caption(caption)
        self._map.set_anchor(self._anchor_sample_id())
        self._map.set_halo(set())
        self._update_badges()
        self._sync_map_visibility()

    def _sync_map_visibility(self, *_args) -> None:
        self._map.set_visible(self._proxy.visible_sample_ids())

    def _update_badges(self) -> None:
        self._map.set_badges(self._samples.hit_sample_ids())

    def _parent_of_segment(self, segment_id: int) -> int | None:
        row = self._conn.execute("SELECT sample_id FROM segments WHERE id = ?", (segment_id,)).fetchone()
        return None if row is None else int(row[0])

    def _anchor_sample_id(self) -> int | None:
        if self._anchor is None:
            return None
        kind, item_id = self._anchor
        if kind == KIND_SAMPLE:
            return int(item_id)
        row = self._conn.execute("SELECT sample_id FROM segments WHERE id = ?", (item_id,)).fetchone()
        return None if row is None else int(row[0])

    def _select_sample(self, sample_id: int) -> None:
        """A click on the map selects the sample in the list (and previews it)."""
        source_row = self._source_row_of.get(sample_id)
        if source_row is None:
            return
        proxy_index = self._proxy.mapFromSource(self._samples.index(source_row, 0))
        if proxy_index.isValid():
            self._table.setCurrentIndex(proxy_index)

    def _play_sample(self, sample_id: int) -> None:
        self._select_sample(sample_id)
        self._play_current()

    def _run_plan(self, plan: RunPlan) -> None:
        """The Recompute tab's Run: its ticked steps in order — Attributes,
        then Map layout — each job's end (`job_ended`) starting the next; a
        stopped or failed step drops the rest."""
        steps: list[tuple[str, str | None]] = []
        if plan.attributes:
            steps.append(("attributes", None))
        if plan.layout:
            steps.append(("layout", plan.layout))
        if plan.captions is not None:
            steps.append(("captions", str(plan.captions)))
        self._plan_steps = steps
        self._advance_plan("", True)

    def _advance_plan(self, _name: str, completed: bool) -> None:
        if not completed:
            if self._plan_steps:
                self.statusBar().showMessage("the remaining Recompute steps were dropped")
            self._plan_steps = []
            return
        while self._plan_steps:
            step, option = self._plan_steps.pop(0)
            if step == "attributes":
                if self._recompute.run_attributes():
                    return                                  # continues from job_ended
                self._plan_steps = []
                return
            if step == "layout":
                if self._run_layout(option or "library"):
                    return
                self._plan_steps = []
                return
            if step == "captions":
                if self._recompute.run_captions(int(option or 0)):
                    return
                self._plan_steps = []
                return

    def _run_layout(self, mode: str) -> bool:
        """§9.6 the *Map layout* step: a full re-fit over the folders in
        scope, or the anchor alone transformed into the existing layout.
        True when a job started."""
        if mode == "anchored":
            if self._anchor is None:
                self.statusBar().showMessage("anchored-only layout needs an anchor (⚓)")
                return False
            kind, item_id = self._anchor
            self._recompute.start_job(
                "place the anchor in the map layout",
                lambda conn, _stop: place_anchor(conn, kind, item_id),
            )
            return True
        weights = self._attributes.weights()
        if not any(weights.values()):
            self.statusBar().showMessage(
                "a layout needs at least one weight above zero (Attributes tab)"
            )
            return False
        scope = tuple(self._recompute.scope_folders())
        if not scope:
            self.statusBar().showMessage(
                "nothing in scope: tick a folder on the Recompute tab first"
            )
            return False
        description = ", ".join(Path(folder).name or folder for folder in scope)
        settings = LayoutSettings(weights=weights, scope=scope, scope_description=description[:80])
        layout_dir = self._layout_dir
        reducer = self._reducer_factory() if self._reducer_factory is not None else None
        self._recompute.start_job(
            "recompute map layout",
            lambda conn, stop: fit_layout(
                conn, settings, layout_dir, reducer=reducer, should_stop=stop
            ),
        )
        return True

    # --- lifecycle ---

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._recompute.running:
            # A job mid-file cannot be cut off (its thread would be destroyed
            # under it), and its completion must not reload a window whose
            # connection is gone (2026-09-07 review): ask it to stop, keep the
            # window, and close when it ends.
            self._close_pending = True
            self._recompute.stop()
            self.statusBar().showMessage("closing after the current file…")
            event.ignore()
            return
        self._preview.stop()
        self._playhead.stop()
        self._waveform.wait_for_load(deliver=False)
        if self._search_thread is not None:
            self._search_thread.wait()           # a model load cannot be interrupted
        if self._feature_thread is not None:
            self._feature_thread.wait()
        self._recompute.index_changed.disconnect(self.reload)
        self._recompute.save_settings()
        self._settings.setValue(SETTINGS_KEY_SPLITTER, self._body.saveState())
        self._settings.setValue(SETTINGS_KEY_PANES, self._panes.saveState())
        self._recompute.shutdown()
        self._conn.close()
        super().closeEvent(event)

    def _close_if_pending(self) -> None:
        if self._close_pending:
            self.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crate", description="Crate — listen and grab.")
    parser.add_argument(
        "--db", type=Path, default=None,
        help=f"index database path (default: {default_db_path()})",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    app = QApplication(sys.argv[:1])
    apply_theme(app)
    window = MainWindow(args.db)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
