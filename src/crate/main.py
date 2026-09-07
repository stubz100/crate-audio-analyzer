"""The application window — Phase 4.5 "listen and grab" plus the Phase 8
Recompute tab (spec §12, §9.6).

Left: a plain sortable, filterable list of samples, the selected sample's
segments underneath, a preview player, native drag-out of either into
Bitwig. Right: the Recompute tab — the only place anything expensive starts,
and only on a button press (§9.6). The window itself computes nothing: it
reads the index and renders a segment on first use. No anchor, no map, no
marker editing yet — those are Phases 6, 7 and 9.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QSortFilterProxyModel, Qt, QUrl
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
    QVBoxLayout,
    QWidget,
)

from .catalog import index_summary, load_samples, load_segments
from .db import default_db_path, open_db
from .listmodel import SORT_ROLE, SampleTableModel, SegmentTableModel
from .recompute import EncoderFactory, RecomputePanel
from .render import default_cache_dir, render_segment

log = logging.getLogger(__name__)

ORG_NAME = "Crate"
APP_NAME = "Crate"
SETTINGS_KEY_AUTOPLAY = "preview/autoplay"
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


class MainWindow(QMainWindow):
    """The application's main window.

    `settings` defaults to the per-user store (the registry on Windows);
    tests pass an INI-backed one so they never touch the user's own values.
    `encoder_factory` lets tests run the Recompute tab against a fake model.
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
        self._current: Path | None = None

        # --- filter ---
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter (file, folder, type, class, tags…)")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._on_filter_changed)

        # --- the list (samples only, §6.4) ---
        self._samples = SampleTableModel()
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._samples)
        self._proxy.setSortRole(SORT_ROLE)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._configure_drag_view(self._table)
        self._table.selectionModel().currentRowChanged.connect(self._on_sample_selected)
        self._table.doubleClicked.connect(lambda _index: self._play_current())

        # --- the selected sample's segments (§6.4 drill-down) ---
        self._segments = SegmentTableModel(self._render)
        self._segment_table = QTableView()
        self._segment_table.setModel(self._segments)
        self._configure_drag_view(self._segment_table)
        self._segment_table.selectionModel().currentRowChanged.connect(self._on_segment_selected)
        self._segment_table.doubleClicked.connect(lambda _index: self._play_current())

        tables = QSplitter(Qt.Orientation.Vertical)
        tables.addWidget(self._table)
        tables.addWidget(self._segment_table)
        tables.setStretchFactor(0, 4)
        tables.setStretchFactor(1, 1)

        # --- transport ---
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
        transport = QHBoxLayout()
        transport.addWidget(self._play_button)
        transport.addWidget(stop_button)
        transport.addWidget(self._autoplay)
        transport.addWidget(self._now_playing, stretch=1)
        transport.addWidget(QLabel("Drag a row into Bitwig ↗"))
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self._toggle_play)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.addWidget(self._filter)
        left_layout.addWidget(tables, stretch=1)
        left_layout.addLayout(transport)

        # --- right panel: the Recompute tab (§9.6); Attributes arrives with Phase 7 ---
        self._recompute = RecomputePanel(self._db_path, self._settings, encoder_factory, self)
        self._recompute.index_changed.connect(self.reload)
        self._close_pending = False
        self._recompute.index_changed.connect(self._close_if_pending)
        tabs = QTabWidget()
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

    # --- data ---

    def reload(self) -> None:
        """Re-read the index — after a Recompute job, or a CLI run outside."""
        self._samples.set_rows(load_samples(self._conn))
        self._segments.set_rows([])
        self._table.resizeColumnsToContents()
        counts = index_summary(self._conn)
        self.statusBar().showMessage(
            f"{counts['samples']} samples · {counts['analysed']} analysed · "
            f"{counts['segments']} segments · {counts['embedded']} embedded · index: {self._db_path}"
        )

    def _render(self, segment_id: int) -> Path:
        return render_segment(self._conn, segment_id, self._cache_dir)

    @staticmethod
    def _configure_drag_view(view: QTableView) -> None:
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        view.setDragEnabled(True)
        view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        view.setDefaultDropAction(Qt.DropAction.CopyAction)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.verticalHeader().setVisible(False)
        view.setAlternatingRowColors(True)

    # --- selection / preview ---

    def _on_sample_selected(self, current, _previous) -> None:
        if not current.isValid():
            return
        row = self._samples.row_at(self._proxy.mapToSource(current))
        self._segments.set_rows(load_segments(self._conn, row.id))
        self._segment_table.resizeColumnsToContents()
        self._current = Path(row.filepath)
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
