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

from PySide6.QtCore import QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .attributes import AttributesPanel
from .catalog import describe_item, index_summary, load_samples, load_segments, load_tags
from .db import default_db_path, open_db
from .embedding import ClapEncoder, EmbedSettings
from .listmodel import ListProxy, SampleTreeModel, SegmentTableModel
from .recompute import EncoderFactory, RecomputePanel
from .render import default_cache_dir, render_segment
from .similarity import KIND_SAMPLE, KIND_SEGMENT, FeatureTable

log = logging.getLogger(__name__)

ORG_NAME = "Crate"
APP_NAME = "Crate"
SETTINGS_KEY_AUTOPLAY = "preview/autoplay"
SETTINGS_KEY_ANCHOR_KIND = "anchor/kind"
SETTINGS_KEY_ANCHOR_ID = "anchor/id"
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

    @property
    def playing(self) -> bool:
        if self._player is None:
            return False
        from PySide6.QtMultimedia import QMediaPlayer

        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState


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
    against a fake model.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        cache_dir: Path | None = None,
        settings: QSettings | None = None,
        encoder_factory: EncoderFactory | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Crate")
        self.resize(1400, 800)
        self._settings = settings if settings is not None else QSettings(ORG_NAME, APP_NAME)
        self._db_path = Path(db_path) if db_path is not None else default_db_path()
        self._cache_dir = cache_dir if cache_dir is not None else default_cache_dir()
        self._conn = open_db(self._db_path)
        self._preview = Preview(self)
        self._encoder_factory = encoder_factory
        self._encoder = None
        self._search_thread: _EmbedTextThread | None = None
        self._pending_search: str | None = None
        self._features: FeatureTable | None = None
        self._current: Path | None = None
        self._current_item: tuple[str, int] | None = None
        self._anchor: tuple[str, int] | None = None
        self._axis = None
        self._axis_by_sample: dict[int, dict[str, float]] = {}
        self._close_pending = False

        # --- quick filter ---
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Quick filter (file, folder, type, class, tags…)")
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

        tables = QSplitter(Qt.Orientation.Vertical)
        tables.addWidget(self._table)
        tables.addWidget(self._segment_table)
        tables.setStretchFactor(0, 4)
        tables.setStretchFactor(1, 1)

        # --- transport + anchor (§9.2, the minimal slice Phase 7 needs) ---
        self._play_button = QPushButton("▶ Play")
        self._play_button.clicked.connect(self._play_current)
        stop_button = QPushButton("■ Stop")
        stop_button.clicked.connect(self._preview.stop)
        self._autoplay = QCheckBox("Auto-play on select")
        self._autoplay.setChecked(self._settings.value(SETTINGS_KEY_AUTOPLAY, True, type=bool))
        self._autoplay.toggled.connect(
            lambda on: self._settings.setValue(SETTINGS_KEY_AUTOPLAY, bool(on))
        )
        self._now_playing = QLabel("")
        self._anchor_button = QPushButton("⚓ Anchor")
        self._anchor_button.setToolTip(
            "Pin the selected sample or hit as the comparison reference (§9.2): "
            "unlocks the Attributes tab's distance ranges and Recompute ranking."
        )
        self._anchor_button.clicked.connect(self._anchor_current)
        self._anchor_label = QLabel("no anchor")
        clear_anchor = QPushButton("✕")
        clear_anchor.setToolTip("Clear the anchor")
        clear_anchor.setFixedWidth(28)
        clear_anchor.clicked.connect(self._clear_anchor)
        transport = QHBoxLayout()
        transport.addWidget(self._play_button)
        transport.addWidget(stop_button)
        transport.addWidget(self._autoplay)
        transport.addWidget(self._now_playing, stretch=1)
        transport.addWidget(self._anchor_button)
        transport.addWidget(self._anchor_label)
        transport.addWidget(clear_anchor)
        transport.addWidget(QLabel("Drag a row into Bitwig ↗"))
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self._toggle_play)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.addWidget(self._filter)
        left_layout.addWidget(tables, stretch=1)
        left_layout.addLayout(transport)

        # --- right panel: Attributes (§9.5) and Recompute (§9.6) ---
        self._attributes = AttributesPanel(self._settings)
        self._attributes.search_requested.connect(self._search)
        self._attributes.search_cleared.connect(self._clear_search)
        self._attributes.criteria_changed.connect(self._proxy.set_criteria)
        self._recompute = RecomputePanel(self._db_path, self._settings, encoder_factory, self)
        self._recompute.index_changed.connect(self.reload)
        self._recompute.index_changed.connect(self._close_if_pending)
        self._recompute.rank_requested.connect(self._rank)
        tabs = QTabWidget()
        tabs.addTab(self._attributes, "Attributes")
        tabs.addTab(self._recompute, "Recompute")
        tabs.setMinimumWidth(420)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.addWidget(left)
        body.addWidget(tabs)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 1)
        body.setSizes([920, 480])
        self.setCentralWidget(body)

        self.reload()
        self._restore_anchor()

    # --- data ---

    def reload(self) -> None:
        """Re-read the index — after a Recompute job, or a CLI run outside.
        Scores are view state and start over; an anchor keeps its place and
        its distances are recomputed on the fresh features."""
        self._samples.set_rows(load_samples(self._conn))
        self._samples.set_similarity(None)
        self._samples.set_match(None)
        self._segments.set_rows([])
        self._features = None
        self._update_score_columns()
        for column in range(len(SampleTreeModel.COLUMNS)):
            self._table.resizeColumnToContents(column)
        counts = index_summary(self._conn)
        self.statusBar().showMessage(
            f"{counts['samples']} samples · {counts['analysed']} analysed · "
            f"{counts['segments']} segments · {counts['embedded']} embedded · index: {self._db_path}"
        )
        if self._anchor is not None and not self._apply_anchor(*self._anchor, announce=False):
            self._clear_anchor()

    def _ensure_features(self) -> FeatureTable:
        if self._features is None:
            self._features = FeatureTable.load(self._conn)
        return self._features

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
        # The drill-down and the chips are the sample's either way — a sub-hit
        # is a segment *of* that sample (2026-09-07 review).
        self._segments.set_rows(load_segments(self._conn, row.id))
        self._segment_table.resizeColumnsToContents()
        self._attributes.show_tags(load_tags(self._conn, row.id))
        if hit is not None:
            try:
                self._current = self._render(hit.segment_id)
            except (ValueError, LookupError, OSError) as exc:
                self.statusBar().showMessage(f"hit cannot be rendered: {exc}")
                return
            self._current_item = (KIND_SEGMENT, hit.segment_id)
            self._now_playing.setText(
                f"hit @ {hit.start_ms / 1000:.3f} s ({hit.end_ms - hit.start_ms} ms) in {row.filename}"
            )
        else:
            self._current = Path(row.filepath)
            self._current_item = (KIND_SAMPLE, row.id)
            self._now_playing.setText(row.filename)
        if self._autoplay.isChecked():
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
        self._now_playing.setText(f"hit @ {seg.start_ms / 1000:.3f} s ({seg.length_ms} ms)")
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

    # --- anchor (§9.2) and ranking (§9.6) ---

    def _anchor_current(self) -> None:
        if self._current_item is None:
            self.statusBar().showMessage("select a sample or a hit first, then anchor it")
            return
        self._apply_anchor(*self._current_item)

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
        self._anchor_label.setText(f"⚓ {label}")
        self._proxy.set_axis_lookup(self._axis_by_sample.get)
        self._attributes.set_anchor_state(True)
        self._recompute.set_ranking_available(True, label)
        if announce:
            self.statusBar().showMessage(
                f"anchored on {label} — distances ready; Recompute ranking (Recompute tab) to rank"
            )
        return True

    def _clear_anchor(self) -> None:
        self._anchor = None
        self._axis = None
        self._axis_by_sample = {}
        self._settings.remove(SETTINGS_KEY_ANCHOR_KIND)
        self._settings.remove(SETTINGS_KEY_ANCHOR_ID)
        self._anchor_label.setText("no anchor")
        self._proxy.set_axis_lookup(lambda _sample_id: None)
        self._attributes.set_anchor_state(False)
        self._recompute.set_ranking_available(False)
        self._samples.set_similarity(None)
        self._update_score_columns()

    def _restore_anchor(self) -> None:
        kind = self._settings.value(SETTINGS_KEY_ANCHOR_KIND, "", type=str)
        item_id = self._settings.value(SETTINGS_KEY_ANCHOR_ID, 0, type=int)
        if kind and item_id and not self._apply_anchor(kind, item_id, announce=False):
            self._clear_anchor()

    def _rank(self, scope: str) -> None:
        """§9.6 *Recompute ranking*: blend the anchor distances with the
        weight bars, over the whole index or the visible rows only."""
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
        sample_ids = self._proxy.visible_sample_ids() if scope == "visible" else None
        scores = features.rank(self._axis, weights, sample_ids)
        self._samples.set_similarity(scores)
        self._update_score_columns()
        self._table.sortByColumn(SampleTreeModel.COL_SIMILARITY, Qt.SortOrder.DescendingOrder)
        self._table.expandAll()
        self.statusBar().showMessage(
            f"ranked {len(scores.sample)} samples against {self._anchor_label.text()} "
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
        scores = self._ensure_features().search(query)
        self._samples.set_match(scores)
        self._update_score_columns()
        self._table.sortByColumn(SampleTreeModel.COL_MATCH, Qt.SortOrder.DescendingOrder)
        self._table.expandAll()
        self.statusBar().showMessage(
            f"“{text}”: {len(scores.sample)} samples scored, "
            f"{len(scores.hits)} hits inside longer samples"
        )

    def _clear_search(self) -> None:
        self._samples.set_match(None)
        self._update_score_columns()
        if self._samples.has_similarity:
            self._table.sortByColumn(SampleTreeModel.COL_SIMILARITY, Qt.SortOrder.DescendingOrder)
            self._table.expandAll()

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
        if self._search_thread is not None:
            self._search_thread.wait()           # a model load cannot be interrupted
        self._recompute.index_changed.disconnect(self.reload)
        self._recompute.save_settings()
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
    window = MainWindow(args.db)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
