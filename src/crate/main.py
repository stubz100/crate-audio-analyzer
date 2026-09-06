"""The application window — Phase 4.5 "listen and grab" (spec §12).

Deliberately minimal: a plain sortable, filterable list of samples, the
selected sample's segments underneath, a preview player, and native
drag-out of either into Bitwig. No anchor, no map, no marker editing — those
are Phases 6–9. Nothing here computes anything (spec §9.6): the window reads
the index the CLI commands built and renders a segment on first use.
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
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .catalog import index_summary, load_samples, load_segments
from .config import DEFAULT_LIBRARY_PATH
from .db import default_db_path, open_db
from .listmodel import SORT_ROLE, SampleTableModel, SegmentTableModel
from .render import default_cache_dir, render_segment

log = logging.getLogger(__name__)

ORG_NAME = "Crate"
APP_NAME = "Crate"
SETTINGS_KEY_LIBRARY_PATH = "library/root_path"
SETTINGS_KEY_AUTOPLAY = "preview/autoplay"


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
    """The application's main window."""

    def __init__(self, db_path: Path | str | None = None, cache_dir: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Crate")
        self.resize(1100, 700)
        self._settings = QSettings(ORG_NAME, APP_NAME)
        stored_path = self._settings.value(SETTINGS_KEY_LIBRARY_PATH, DEFAULT_LIBRARY_PATH)
        self._library_path = Path(stored_path)
        self._db_path = Path(db_path) if db_path is not None else default_db_path()
        self._cache_dir = cache_dir if cache_dir is not None else default_cache_dir()
        self._conn = open_db(self._db_path)
        self._preview = Preview(self)
        self._current: Path | None = None

        # --- header: library, index, filter ---
        self._path_label = QLabel(str(self._library_path))
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._on_browse_clicked)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Sample library:"))
        path_row.addWidget(self._path_label, stretch=1)
        path_row.addWidget(browse_button)

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

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._table)
        splitter.addWidget(self._segment_table)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)

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

        layout = QVBoxLayout()
        layout.addLayout(path_row)
        layout.addWidget(self._filter)
        layout.addWidget(splitter, stretch=1)
        layout.addLayout(transport)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self.reload()

    # --- data ---

    def reload(self) -> None:
        """Re-read the index. Cheap enough to call after a CLI run."""
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

    # --- library path (Phase 0) ---

    def _set_library_path(self, path: Path) -> None:
        self._library_path = path
        self._path_label.setText(str(path))
        self._settings.setValue(SETTINGS_KEY_LIBRARY_PATH, str(path))

    def _on_browse_clicked(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose sample library folder", str(self._library_path)
        )
        if chosen:
            self._set_library_path(Path(chosen))

    def closeEvent(self, event) -> None:  # noqa: N802
        self._preview.stop()
        self._conn.close()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crate", description="Crate — listen and grab.")
    parser.add_argument(
        "--db", type=Path, default=None,
        help=f"index database path (default: {default_db_path()})",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    app = QApplication(sys.argv[:1])
    window = MainWindow(args.db)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
