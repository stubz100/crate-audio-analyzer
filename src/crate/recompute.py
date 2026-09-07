"""The Recompute tab (spec §9.6, Phase 8): the only place anything expensive
starts, and only on a button press.

Here now: **Rescan library** (node A over the whole root — cheap, no decode),
the **folder-scope list** that defines what library scope covers, **Recompute
attributes** (new/changed only, or force full re-index) with its settings
panel, a **Stop** button, and a log. Waiting for their phases: *Recompute
ranking* (needs the Similarity column, Phase 7), *Recompute map layout*
(Phase 6) and the *anchored-only* scope (needs an anchor, Phase 9).

The work runs on a `QThread` with its own SQLite connection — one process
(§10); WAL lets the window keep reading meanwhile (`db.open_db`). Progress is
whatever the pipeline logs on the `crate` logger, relayed to the log panel
through a queued signal.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .analysis import ONE_SHOT_MAX_DURATION_S
from .config import DEFAULT_LIBRARY_PATH
from .db import open_db
from .embedding import EmbedSettings, Encoder
from .jobs import RecomputeSettings, recompute_attributes
from .scanner import scan_library
from .segmentation import (
    BOUNDARY_MODES,
    DEFAULT_MAX_LENGTH_S,
    DEFAULT_MAX_SEGMENTS,
    DEFAULT_MIN_LENGTH_S,
    DEFAULT_SENSITIVITY,
    UNITS,
    SegmentationSettings,
)

log = logging.getLogger(__name__)

SETTINGS_KEY_LIBRARY_PATH = "library/root_path"
_KEY = "recompute/"

EncoderFactory = Callable[[EmbedSettings], Encoder]
Job = Callable[[sqlite3.Connection, Callable[[], bool]], object]


class _LogRelay(QObject):
    message = Signal(str)


class _LogHandler(logging.Handler):
    """Forwards the `crate` logger's records to the panel while a job runs.
    Records arrive on the worker thread; the relay's signal crosses to the
    GUI thread as a queued connection."""

    def __init__(self, relay: _LogRelay) -> None:
        super().__init__(logging.INFO)
        self._relay = relay
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._relay.message.emit(self.format(record))
        except Exception:  # pragma: no cover - logging must never take the job down
            self.handleError(record)


class JobThread(QThread):
    """Runs `job(conn, should_stop)` on a connection of its own and reports
    the formatted summary, or the error, back to the GUI thread."""

    succeeded = Signal(str, str)   # job name, formatted summary
    failed = Signal(str, str)      # job name, error text

    def __init__(self, db_path: Path, name: str, job: Job, parent=None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._name = name
        self._job = job
        self._stop_requested = False

    @property
    def name(self) -> str:
        return self._name

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:  # worker thread
        try:
            conn = open_db(self._db_path)
            try:
                result = self._job(conn, lambda: self._stop_requested)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - reported to the panel, never lost
            log.warning("%s failed: %s: %s", self._name, type(exc).__name__, exc)
            self.failed.emit(self._name, f"{type(exc).__name__}: {exc}")
            return
        self.succeeded.emit(self._name, result.format())


class RecomputePanel(QWidget):
    """The tab. `index_changed` fires when a job ends — stopped or failed
    included, since a per-file-committing stage leaves real rows behind."""

    index_changed = Signal()

    def __init__(
        self,
        db_path: Path | str,
        settings: QSettings,
        encoder_factory: EncoderFactory | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._db_path = Path(db_path)
        self._settings = settings
        self._encoder_factory = encoder_factory
        self._thread: JobThread | None = None
        self._relay = _LogRelay(self)
        self._relay.message.connect(self._append_log)
        self._handler = _LogHandler(self._relay)
        crate_logger = logging.getLogger("crate")
        if crate_logger.level == logging.NOTSET:
            crate_logger.setLevel(logging.INFO)  # progress lines regardless of root config
        self._library_root = Path(settings.value(SETTINGS_KEY_LIBRARY_PATH, DEFAULT_LIBRARY_PATH))

        # --- library root + rescan ---
        self._root_label = QLabel(str(self._library_root))
        self._root_label.setWordWrap(True)
        self._root_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._browse_button = QPushButton("Browse…")
        self._browse_button.clicked.connect(self._on_browse)
        root_row = QHBoxLayout()
        root_row.addWidget(self._root_label, stretch=1)
        root_row.addWidget(self._browse_button)
        self._rescan_button = QPushButton("Rescan library")
        self._rescan_button.setToolTip(
            "Walk the whole root: new, changed, moved and removed files; flag rows "
            "whose content changed. No decode, no model (§9.6)."
        )
        self._rescan_button.clicked.connect(self.run_rescan)
        root_group = QGroupBox("Library root")
        root_layout = QVBoxLayout(root_group)
        root_layout.addLayout(root_row)
        root_layout.addWidget(self._rescan_button)

        # --- folder-scope list ---
        self._scope_list = QListWidget()
        self._scope_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._scope_list.setMaximumHeight(110)   # the settings + Run must stay above the fold
        self._scope_list.setToolTip(
            "Recompute attributes runs over the files under these folders and "
            "nothing else. Add the root itself for the whole library."
        )
        for folder in self._stored_scope():
            self._scope_list.addItem(folder)
        self._add_button = QPushButton("Add folder…")
        self._add_button.clicked.connect(self._on_add_folder)
        self._add_root_button = QPushButton("Add root")
        self._add_root_button.clicked.connect(lambda: self.add_scope_folder(self._library_root))
        self._remove_button = QPushButton("Remove")
        self._remove_button.clicked.connect(self._on_remove_folders)
        scope_buttons = QHBoxLayout()
        scope_buttons.addWidget(self._add_button)
        scope_buttons.addWidget(self._add_root_button)
        scope_buttons.addWidget(self._remove_button)
        scope_group = QGroupBox("Library scope")
        scope_layout = QVBoxLayout(scope_group)
        scope_hint = QLabel("Recompute attributes runs over these folders only:")
        scope_hint.setWordWrap(True)
        scope_layout.addWidget(scope_hint)
        scope_layout.addWidget(self._scope_list)
        scope_layout.addLayout(scope_buttons)

        # --- recompute attributes: mode + settings ---
        v = self._settings.value
        self._changed_only = QRadioButton("New/changed only")
        self._force_full = QRadioButton("Force full re-index")
        force_full = v(_KEY + "force_full", False, type=bool)
        (self._force_full if force_full else self._changed_only).setChecked(True)
        mode_row = QHBoxLayout()
        mode_row.addWidget(self._changed_only)
        mode_row.addWidget(self._force_full)

        self._embed_segments = QCheckBox()
        self._embed_segments.setChecked(v(_KEY + "embed_segments", True, type=bool))
        self._min_embed_ms = QSpinBox()
        self._min_embed_ms.setRange(0, 10_000)
        self._min_embed_ms.setSuffix(" ms")
        self._min_embed_ms.setValue(v(_KEY + "min_segment_length_ms", 200, type=int))
        self._confidence = QDoubleSpinBox()
        self._confidence.setRange(0.0, 1.0)
        self._confidence.setSingleStep(0.05)
        self._confidence.setDecimals(2)
        self._confidence.setValue(v(_KEY + "confidence_threshold", 0.5, type=float))
        self._sensitivity = QDoubleSpinBox()
        self._sensitivity.setRange(0.01, 1.0)
        self._sensitivity.setSingleStep(0.05)
        self._sensitivity.setDecimals(2)
        self._sensitivity.setValue(v(_KEY + "sensitivity", DEFAULT_SENSITIVITY, type=float))
        self._min_length = QDoubleSpinBox()
        self._min_length.setRange(0.0, 1000.0)
        self._min_length.setDecimals(3)
        self._min_length.setSingleStep(0.01)
        self._min_length.setValue(v(_KEY + "min_length", DEFAULT_MIN_LENGTH_S, type=float))
        self._min_unit = QComboBox()
        self._min_unit.addItems(list(UNITS))
        self._min_unit.setCurrentText(v(_KEY + "min_length_unit", "s", type=str))
        self._max_length = QDoubleSpinBox()
        self._max_length.setRange(0.0, 1000.0)
        self._max_length.setDecimals(3)
        self._max_length.setSingleStep(0.1)
        self._max_length.setValue(v(_KEY + "max_length", DEFAULT_MAX_LENGTH_S, type=float))
        self._max_unit = QComboBox()
        self._max_unit.addItems(list(UNITS))
        self._max_unit.setCurrentText(v(_KEY + "max_length_unit", "s", type=str))
        self._boundary = QComboBox()
        self._boundary.addItems(list(BOUNDARY_MODES))
        self._boundary.setCurrentText(v(_KEY + "boundary_mode", BOUNDARY_MODES[0], type=str))
        self._max_segments = QSpinBox()
        self._max_segments.setRange(1, 100)
        self._max_segments.setValue(v(_KEY + "max_segments", DEFAULT_MAX_SEGMENTS, type=int))
        self._one_shot_cap = QCheckBox("on")
        self._one_shot_cap.setChecked(v(_KEY + "one_shot_cap", True, type=bool))
        self._one_shot_seconds = QDoubleSpinBox()
        self._one_shot_seconds.setRange(0.1, 600.0)
        self._one_shot_seconds.setDecimals(1)
        self._one_shot_seconds.setSuffix(" s")
        self._one_shot_seconds.setValue(
            v(_KEY + "one_shot_max_duration_s", ONE_SHOT_MAX_DURATION_S, type=float)
        )
        self._one_shot_seconds.setEnabled(self._one_shot_cap.isChecked())
        self._one_shot_cap.toggled.connect(self._one_shot_seconds.setEnabled)
        self._qwen = QCheckBox("off")
        self._qwen.setEnabled(False)
        self._qwen.setToolTip("Phase 5 (spec §5.2): opt-in, CPU-only, not built yet.")

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.addRow("Embed segments", self._embed_segments)
        form.addRow("Min length for segment embedding", self._min_embed_ms)
        form.addRow("Facet A confidence threshold", self._confidence)
        form.addRow("Transient sensitivity", self._sensitivity)
        form.addRow("Min segment length", _pair(self._min_length, self._min_unit))
        form.addRow("Max segment length", _pair(self._max_length, self._max_unit))
        form.addRow("Segmentation boundary mode", self._boundary)
        form.addRow("Max segments per sample", self._max_segments)
        form.addRow("One-shot max duration", _pair(self._one_shot_cap, self._one_shot_seconds))
        form.addRow("Qwen2-Audio captioning", self._qwen)

        self._recompute_button = QPushButton("Recompute attributes")
        self._recompute_button.setToolTip(
            "Analysis, segmentation and embedding over the library scope, in that order."
        )
        self._recompute_button.clicked.connect(self.run_recompute)
        self._stop_button = QPushButton("Stop")
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self.stop)
        run_row = QHBoxLayout()
        run_row.addWidget(self._recompute_button, stretch=1)
        run_row.addWidget(self._stop_button)
        attributes_group = QGroupBox("Recompute attributes")
        attributes_layout = QVBoxLayout(attributes_group)
        attributes_layout.addLayout(mode_row)
        attributes_layout.addLayout(run_row)      # the action first; its settings below
        attributes_layout.addLayout(form)

        # --- log ---
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(5000)
        self._log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._log.setPlaceholderText("Nothing runs until you press a button (§9.6).")

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.addWidget(root_group)
        controls_layout.addWidget(scope_group)
        controls_layout.addWidget(attributes_group)
        controls_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(controls)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(scroll)
        splitter.addWidget(self._log)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(splitter)

    # --- state ---

    @property
    def running(self) -> bool:
        return self._thread is not None

    @property
    def library_root(self) -> Path:
        return self._library_root

    def set_library_root(self, path: Path | str) -> None:
        self._library_root = Path(path)
        self._root_label.setText(str(self._library_root))
        self._settings.setValue(SETTINGS_KEY_LIBRARY_PATH, str(self._library_root))

    def scope_folders(self) -> list[str]:
        return [self._scope_list.item(i).text() for i in range(self._scope_list.count())]

    def add_scope_folder(self, path: Path | str) -> None:
        folder = Path(path)
        text = str(folder)
        if text in self.scope_folders():
            return
        self._scope_list.addItem(text)
        self._save_scope()
        if not folder.resolve().is_relative_to(self._library_root.resolve()):
            self._append_log(
                f"note: {text} is outside the library root, and Rescan indexes the "
                "root only — nothing will be found there"
            )

    def log_text(self) -> str:
        return self._log.toPlainText()

    def collect_settings(self) -> RecomputeSettings:
        """The controls as one settings object; raises ValueError on a
        combination the pipeline refuses (e.g. min length ≥ max length)."""
        segmentation = SegmentationSettings(
            sensitivity=self._sensitivity.value(),
            boundary_mode=self._boundary.currentText(),
            min_length=self._min_length.value(),
            min_length_unit=self._min_unit.currentText(),
            max_length=self._max_length.value(),
            max_length_unit=self._max_unit.currentText(),
            max_segments=self._max_segments.value(),
        )
        embedding = EmbedSettings(
            embed_segments=self._embed_segments.isChecked(),
            min_segment_length_ms=self._min_embed_ms.value(),
            confidence_threshold=self._confidence.value(),
        )
        cap = self._one_shot_seconds.value() if self._one_shot_cap.isChecked() else None
        return RecomputeSettings(
            scope=tuple(self.scope_folders()),
            force_full=self._force_full.isChecked(),
            one_shot_max_duration_s=cap,
            segmentation=segmentation,
            embedding=embedding,
        )

    def save_settings(self) -> None:
        s = self._settings.setValue
        s(_KEY + "force_full", self._force_full.isChecked())
        s(_KEY + "embed_segments", self._embed_segments.isChecked())
        s(_KEY + "min_segment_length_ms", self._min_embed_ms.value())
        s(_KEY + "confidence_threshold", self._confidence.value())
        s(_KEY + "sensitivity", self._sensitivity.value())
        s(_KEY + "min_length", self._min_length.value())
        s(_KEY + "min_length_unit", self._min_unit.currentText())
        s(_KEY + "max_length", self._max_length.value())
        s(_KEY + "max_length_unit", self._max_unit.currentText())
        s(_KEY + "boundary_mode", self._boundary.currentText())
        s(_KEY + "max_segments", self._max_segments.value())
        s(_KEY + "one_shot_cap", self._one_shot_cap.isChecked())
        s(_KEY + "one_shot_max_duration_s", self._one_shot_seconds.value())
        self._save_scope()

    # --- actions (§9.6) ---

    def run_rescan(self) -> None:
        root = self._library_root
        if not root.is_dir():
            self._append_log(f"library root does not exist: {root}")
            return
        self._start("rescan library", lambda conn, stop: scan_library(conn, root, should_stop=stop))

    def run_recompute(self) -> None:
        try:
            settings = self.collect_settings()
        except ValueError as exc:
            self._append_log(f"settings: {exc}")
            return
        if not settings.scope:
            self._append_log(
                "library scope is empty: add a folder (or the root itself) before "
                "recomputing — nothing is implicit (§9.6)"
            )
            return
        self.save_settings()
        encoder = self._encoder_factory(settings.embedding) if self._encoder_factory else None
        self._start(
            "recompute attributes",
            lambda conn, stop: recompute_attributes(
                conn, settings, should_stop=stop, encoder=encoder
            ),
        )

    def stop(self) -> None:
        if self._thread is None:
            return
        self._thread.request_stop()
        self._stop_button.setEnabled(False)
        self._append_log("stop requested: finishing the current file")

    def shutdown(self) -> None:
        """Last resort for a caller tearing the panel down while a job runs:
        ask it to stop and wait — unbounded, because the stop lands after the
        current file and a thread destroyed mid-run takes the process down.
        The window itself defers its close instead (`MainWindow.closeEvent`)."""
        thread = self._thread
        if thread is None:
            return
        thread.request_stop()
        thread.wait()
        logging.getLogger("crate").removeHandler(self._handler)

    # --- plumbing ---

    def _start(self, name: str, job: Job) -> None:
        if self.running:
            self._append_log("a job is already running")
            return
        self._append_log(f"— {name} —")
        logging.getLogger("crate").addHandler(self._handler)
        thread = JobThread(self._db_path, name, job, self)
        thread.succeeded.connect(self._on_succeeded)
        thread.failed.connect(self._on_failed)
        thread.finished.connect(self._on_finished)
        self._thread = thread
        self._set_running(True)
        thread.start()

    def _on_succeeded(self, name: str, text: str) -> None:
        self._append_log(text)

    def _on_failed(self, name: str, error: str) -> None:
        self._append_log(f"{name} failed: {error}")

    def _on_finished(self) -> None:
        logging.getLogger("crate").removeHandler(self._handler)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.deleteLater()
        self._set_running(False)
        self.index_changed.emit()

    def _set_running(self, running: bool) -> None:
        for button in (
            self._rescan_button, self._recompute_button, self._browse_button,
            self._add_button, self._add_root_button, self._remove_button,
        ):
            button.setEnabled(not running)
        self._stop_button.setEnabled(running)

    def _append_log(self, text: str) -> None:
        self._log.appendPlainText(text)

    def _stored_scope(self) -> list[str]:
        raw = self._settings.value(_KEY + "scope", "", type=str)
        return [line for line in raw.split("\n") if line]

    def _save_scope(self) -> None:
        self._settings.setValue(_KEY + "scope", "\n".join(self.scope_folders()))

    def _on_browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose sample library folder", str(self._library_root)
        )
        if chosen:
            self.set_library_root(chosen)

    def _on_add_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Add a folder to the library scope", str(self._library_root)
        )
        if chosen:
            self.add_scope_folder(chosen)

    def _on_remove_folders(self) -> None:
        for item in self._scope_list.selectedItems():
            self._scope_list.takeItem(self._scope_list.row(item))
        self._save_scope()


def _pair(left: QWidget, right: QWidget) -> QWidget:
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(left)
    row.addWidget(right, stretch=1)
    return box
