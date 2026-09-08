"""The Recompute tab (spec §9.6, Phase 8): the only place anything expensive
starts, and only on a button press.

Two panels since 2026-09-08 (the user's steer, after the folder-scope list
and the library root had grown into two things and three Recompute buttons
into a puzzle):

**Library** — the folders the index knows (`library.py`, the `libraries`
table), each with two flags: *Root* (at most one: the library's home) and
*In scope* (shown in the list and map, walked by Rescan, covered by
Recompute; off = dormant, rows kept). *Add folder* scans a folder in,
*Remove folder* deletes its samples from the index (asked first), *Rescan*
walks the folders in scope.

**Recompute** — the three things that can be computed, as ticked steps run
in order by one *Run*: **Attributes** (analysis, segmentation, CLAP
embedding — the expensive stage, with its settings below), **Map layout**
(the projection, whole scope or the anchor alone), **Ranking** (the
Similarity column against the anchor — cheap, needs an anchor). *Stop* ends
the current step after its current file and drops the rest.

The work runs on a `QThread` with its own SQLite connection — one process
(§10); WAL lets the window keep reading meanwhile (`db.open_db`). Progress is
whatever the pipeline logs on the `crate` logger, relayed to the log panel
through a queued signal.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
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
    QHeaderView,
    QLabel,
    QLayout,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .analysis import ONE_SHOT_MAX_DURATION_S
from .config import DEFAULT_LIBRARY_PATH
from .db import open_db
from .embedding import EmbedSettings, Encoder
from .jobs import RecomputeSettings, recompute_attributes
from .library import (
    add_library,
    is_inside,
    list_libraries,
    outermost,
    remove_library,
    root_path,
    scope_paths,
    seed_libraries,
    set_in_scope,
    set_root,
)
from .scanner import scan_library
from .theme import SqueezableWidget
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

SETTINGS_KEY_LIBRARY_PATH = "library/root_path"   # pre-2026-09-08 settings: a seed for the folder list
# Measured 2026-09-07 on 600 short files, 32 cores: 8 workers 3× faster than one,
# 16 slower than 8 (process start-up and per-file hand-off outweigh the work).
DEFAULT_WORKERS = max(1, min(8, (os.cpu_count() or 2) // 2))
_KEY = "recompute/"
_COL_ROOT, _COL_SCOPE, _COL_PATH, _COL_FILES = range(4)

EncoderFactory = Callable[[EmbedSettings], Encoder]
Job = Callable[[sqlite3.Connection, Callable[[], bool]], object]


@dataclass(frozen=True)
class RunPlan:
    """What one press of Run asks for, in the order it runs (§9.6)."""

    attributes: bool = False
    layout: str | None = None    # "library" (re-fit over the scope) | "anchored" (place the anchor)
    ranking: str | None = None   # "whole" (the scope) | "visible" (the filtered rows)

    @property
    def empty(self) -> bool:
        return not (self.attributes or self.layout or self.ranking)


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
        self._failed = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def completed(self) -> bool:
        """Ran to the end: neither failed nor asked to stop."""
        return not (self._failed or self._stop_requested)

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
            self._failed = True
            log.warning("%s failed: %s: %s", self._name, type(exc).__name__, exc)
            self.failed.emit(self._name, f"{type(exc).__name__}: {exc}")
            return
        self.succeeded.emit(self._name, result.format())


class _ScanReport:
    """Rescan's summary over several folders, one scanner summary each."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def format(self) -> str:
        return "\n".join(self.parts) if self.parts else "nothing scanned"


class RecomputePanel(QWidget):
    """The tab. `index_changed` fires when a job ends — stopped or failed
    included, since a per-file-committing stage leaves real rows behind;
    `scope_changed` when a folder's in-scope flag flips; `run_requested`
    hands the window a `RunPlan`; `job_ended` (name, completed) lets the
    window carry the plan on to its next step."""

    index_changed = Signal()
    scope_changed = Signal()
    run_requested = Signal(object)
    job_ended = Signal(str, bool)

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
        self._rank_available = False
        self._refreshing = False
        self._relay = _LogRelay(self)
        self._relay.message.connect(self._append_log)
        self._handler = _LogHandler(self._relay)
        crate_logger = logging.getLogger("crate")
        if crate_logger.level == logging.NOTSET:
            crate_logger.setLevel(logging.INFO)  # progress lines regardless of root config
        self._conn = open_db(self._db_path)
        v = self._settings.value
        seeded = seed_libraries(
            self._conn,
            root_hint=v(SETTINGS_KEY_LIBRARY_PATH, None),
            scope_hint=[line for line in v(_KEY + "scope", "", type=str).split("\n") if line],
        )

        # --- Library: the folders the index knows, their two flags, and what manages them ---
        self._folders = QTreeWidget()
        self._folders.setHeaderLabels(["Root", "In scope", "Folder", "Files"])
        self._folders.setRootIsDecorated(False)
        self._folders.setUniformRowHeights(True)
        self._folders.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._folders.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._folders.header()
        header.setSectionResizeMode(_COL_ROOT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_SCOPE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_PATH, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_FILES, QHeaderView.ResizeMode.ResizeToContents)
        self._folders.setMaximumHeight(170)
        self._folders.setToolTip(
            "Root: the library's home folder (one at a time) — where Add folder starts.\n"
            "In scope: shown in the list and on the map, walked by Rescan, covered by Recompute.\n"
            "Unticked = dormant: its samples stay in the index, untouched, until ticked again."
        )
        self._folders.itemChanged.connect(self._on_folder_item_changed)
        self._rescan_button = QPushButton("Rescan")
        self._rescan_button.setToolTip(
            "Walk the folders in scope: new, changed, moved and removed files; rows whose "
            "content changed are flagged. No decode, no model (§9.6)."
        )
        self._rescan_button.clicked.connect(self.run_rescan)
        self._add_button = QPushButton("Add folder…")
        self._add_button.setToolTip("Choose a folder and scan it into the index (in scope).")
        self._add_button.clicked.connect(self._on_add_folder)
        self._remove_button = QPushButton("Remove folder")
        self._remove_button.setToolTip(
            "Delete the selected folder's samples — with their analysis, segments and "
            "embeddings — from the index. Files on disk are untouched. Asks first."
        )
        self._remove_button.clicked.connect(self._on_remove_folder)
        folder_buttons = QHBoxLayout()
        folder_buttons.addWidget(self._rescan_button)
        folder_buttons.addWidget(self._add_button)
        folder_buttons.addWidget(self._remove_button)
        library_group = QGroupBox("Library")
        library_layout = QVBoxLayout(library_group)
        library_hint = QLabel(
            "Tick a folder to put it in scope: the list, the map, Rescan and Recompute "
            "cover the ticked folders. Unticked folders stay in the index, dormant."
        )
        library_hint.setWordWrap(True)
        library_hint.setObjectName("caption")
        library_layout.addWidget(library_hint)
        library_layout.addWidget(self._folders)
        library_layout.addLayout(folder_buttons)

        # --- Recompute: three steps, one Run ---
        self._step_attributes = QCheckBox("Attributes")
        self._step_attributes.setChecked(v(_KEY + "step_attributes", True, type=bool))
        self._changed_only = QRadioButton("new/changed only")
        self._force_full = QRadioButton("everything again")
        (self._force_full if v(_KEY + "force_full", False, type=bool) else self._changed_only).setChecked(True)
        self._step_layout = QCheckBox("Map layout")
        self._step_layout.setChecked(v(_KEY + "step_layout", False, type=bool))
        self._layout_library = QRadioButton("whole scope (re-fit)")
        self._layout_anchored = QRadioButton("anchor only (place it)")
        (self._layout_anchored if v(_KEY + "layout_mode", "library", type=str) == "anchored"
         else self._layout_library).setChecked(True)
        self._step_ranking = QCheckBox("Ranking")
        self._step_ranking.setChecked(v(_KEY + "step_ranking", False, type=bool))
        self._rank_whole = QRadioButton("whole scope")
        self._rank_visible = QRadioButton("visible rows only")
        (self._rank_visible if v(_KEY + "rank_scope", "whole", type=str) == "visible"
         else self._rank_whole).setChecked(True)
        self._rank_note = QLabel("")
        self._rank_note.setObjectName("caption")
        self._rank_note.setWordWrap(True)

        steps = QVBoxLayout()
        steps.setSpacing(2)
        steps.addLayout(_step(
            self._step_attributes,
            "analysis, segmentation and CLAP embedding of the files in scope — the expensive "
            "stage; its settings are below",
            [self._changed_only, self._force_full],
        ))
        steps.addLayout(_step(
            self._step_layout,
            "project the samples in scope onto the map under the weight bars (UMAP); "
            "\"anchor only\" places the anchor into the existing layout instead",
            [self._layout_library, self._layout_anchored],
        ))
        steps.addLayout(_step(
            self._step_ranking,
            "the Similarity column: every sample's distance to the anchor, blended by the "
            "weight bars — cheap, needs an anchor",
            [self._rank_whole, self._rank_visible],
        ))
        steps.addWidget(self._rank_note)

        self._run_button = QPushButton("Run")
        self._run_button.setToolTip("Run the ticked steps, in order: Attributes → Map layout → Ranking.")
        self._run_button.clicked.connect(self.run)
        self._stop_button = QPushButton("Stop")
        self._stop_button.setEnabled(False)
        self._stop_button.setToolTip("End the running step after its current file; later steps are dropped.")
        self._stop_button.clicked.connect(self.stop)
        run_row = QHBoxLayout()
        run_row.addWidget(self._run_button, stretch=1)
        run_row.addWidget(self._stop_button)

        # --- the Attributes step's settings ---
        self._embed_segments = QCheckBox()
        self._embed_segments.setChecked(v(_KEY + "embed_segments", True, type=bool))
        self._embed_segments.setToolTip(
            "The per-segment CLAP pass (§9.6), the library's real cost multiplier. The 10-s "
            "windows a long file is embedded through are kept as searchable hits regardless: "
            "their vectors come free with the whole-file one (§6.4)."
        )
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
        self._workers = QSpinBox()
        self._workers.setRange(1, max(1, os.cpu_count() or 1))
        self._workers.setValue(v(_KEY + "workers", DEFAULT_WORKERS, type=int))
        self._workers.setToolTip(
            "Worker processes for analysis and segmentation (this machine has "
            f"{os.cpu_count() or 1} cores). Embedding uses the model's own threads."
        )
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
        form.addRow("Worker processes", self._workers)
        form.addRow("Qwen2-Audio captioning", self._qwen)
        settings_label = QLabel("Settings for the Attributes step")
        settings_label.setObjectName("caption")

        recompute_group = QGroupBox("Recompute")
        recompute_layout = QVBoxLayout(recompute_group)
        recompute_layout.addLayout(steps)
        recompute_layout.addLayout(run_row)
        recompute_layout.addWidget(settings_label)
        recompute_layout.addLayout(form)
        self.set_ranking_available(False)

        # --- log ---
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(5000)
        self._log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._log.setPlaceholderText("Nothing runs until you press a button (§9.6).")

        controls = SqueezableWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        controls_layout.addWidget(library_group)
        controls_layout.addWidget(recompute_group)
        controls_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(controls)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(scroll)
        splitter.addWidget(self._log)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(splitter)

        self.refresh_folders()
        if seeded:
            self._append_log(
                f"folder list started from the index and the old settings: {seeded} folder(s) — "
                "tick In scope on the ones to work with"
            )

    # --- state ---

    @property
    def running(self) -> bool:
        return self._thread is not None

    @property
    def library_root(self) -> str | None:
        return root_path(self._conn)

    def scope_folders(self) -> list[str]:
        """The folders in scope, in path order (empty: none ticked, or none known)."""
        return list(scope_paths(self._conn) or ())

    def folder_paths(self) -> list[str]:
        return [lib.path for lib in list_libraries(self._conn)]

    def set_ranking_available(self, available: bool, anchor_label: str = "") -> None:
        """The window tells the tab whether there is an anchor to rank against."""
        self._rank_available = available
        self._step_ranking.setEnabled(available)
        self._rank_whole.setEnabled(available)
        self._rank_visible.setEnabled(available)
        self._layout_anchored.setEnabled(available)
        if not available and self._layout_anchored.isChecked():
            self._layout_library.setChecked(True)
        self._rank_note.setText(
            f"anchor: {anchor_label}" if available
            else "Ranking needs an anchor: select a sample or a hit in the list and press ⚓ Anchor."
        )

    def log_text(self) -> str:
        return self._log.toPlainText()

    def plan(self) -> RunPlan:
        """The ticked steps as the window will run them."""
        layout = None
        if self._step_layout.isChecked():
            layout = "anchored" if self._layout_anchored.isChecked() and self._rank_available else "library"
        ranking = None
        if self._step_ranking.isChecked() and self._rank_available:
            ranking = "visible" if self._rank_visible.isChecked() else "whole"
        return RunPlan(self._step_attributes.isChecked(), layout, ranking)

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
            workers=self._workers.value(),
            segmentation=segmentation,
            embedding=embedding,
        )

    def save_settings(self) -> None:
        s = self._settings.setValue
        s(_KEY + "step_attributes", self._step_attributes.isChecked())
        s(_KEY + "step_layout", self._step_layout.isChecked())
        s(_KEY + "step_ranking", self._step_ranking.isChecked())
        s(_KEY + "layout_mode", "anchored" if self._layout_anchored.isChecked() else "library")
        s(_KEY + "rank_scope", "visible" if self._rank_visible.isChecked() else "whole")
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
        s(_KEY + "workers", self._workers.value())

    # --- the folder list ---

    def refresh_folders(self) -> None:
        """Re-read the `libraries` table into the tree."""
        self._refreshing = True
        try:
            selected = self.selected_folder()
            self._folders.clear()
            for lib in list_libraries(self._conn):
                item = QTreeWidgetItem(["", "", lib.path, str(lib.sample_count)])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(_COL_ROOT, Qt.CheckState.Checked if lib.is_root else Qt.CheckState.Unchecked)
                item.setCheckState(_COL_SCOPE, Qt.CheckState.Checked if lib.in_scope else Qt.CheckState.Unchecked)
                item.setData(_COL_PATH, Qt.ItemDataRole.UserRole, lib.path)
                item.setTextAlignment(_COL_FILES, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                scanned = lib.last_scanned_at[:16].replace("T", " ") if lib.last_scanned_at else "never"
                item.setToolTip(_COL_PATH, f"{lib.path}\nlast scanned: {scanned}")
                self._style_scope(item, lib.in_scope)
                self._folders.addTopLevelItem(item)
                if lib.path == selected:
                    self._folders.setCurrentItem(item)
        finally:
            self._refreshing = False

    def selected_folder(self) -> str | None:
        item = self._folders.currentItem()
        return None if item is None else item.data(_COL_PATH, Qt.ItemDataRole.UserRole)

    def set_root(self, path: Path | str | None) -> None:
        set_root(self._conn, path)
        self.refresh_folders()

    def set_in_scope(self, path: Path | str, in_scope: bool) -> None:
        set_in_scope(self._conn, path, in_scope)
        self.refresh_folders()
        self.scope_changed.emit()

    def add_folder(self, path: Path | str) -> bool:
        """Register a folder, in scope, and scan it in (a job). False if it
        does not exist or a job is running."""
        folder = Path(path)
        if not folder.is_dir():
            self._append_log(f"not a folder: {folder}")
            return False
        if self.running:
            self._append_log("a job is already running")
            return False
        text = add_library(self._conn, folder)
        self.refresh_folders()
        self.scope_changed.emit()
        root = self.library_root
        if root is not None and not is_inside(text, root):
            self._append_log(f"note: {text} lies outside the root {root}")
        self._start(
            f"scan {folder.name or text}",
            lambda conn, stop: scan_library(conn, text, should_stop=stop),
        )
        return True

    def remove_folder(self, path: Path | str | None = None, confirm: bool = True) -> bool:
        """Delete a folder's samples from the index and forget it — after
        asking, unless `confirm` is False (tests)."""
        text = path if path is not None else self.selected_folder()
        if text is None:
            self._append_log("select a folder to remove")
            return False
        if self.running:
            self._append_log("a job is already running")
            return False
        text = str(text)
        count = next((lib.sample_count for lib in list_libraries(self._conn) if lib.path == text), 0)
        if confirm:
            answer = QMessageBox.question(
                self, "Remove folder from the index",
                f"Remove {text} from the index?\n\nThis deletes its {count} samples with their analysis, "
                "segments, embeddings and map positions. Files on disk are not touched; adding the "
                "folder again means recomputing everything.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        self._start(f"remove {Path(text).name or text}", lambda conn, _stop: remove_library(conn, text))
        return True

    # --- actions (§9.6) ---

    def run_rescan(self) -> bool:
        """Walk the folders in scope (each once: a folder inside another
        ticked folder is covered by the outer walk)."""
        folders = outermost(self.scope_folders())
        if not folders:
            self._append_log("nothing in scope: tick a folder (or add one) before rescanning")
            return False
        missing = [f for f in folders if not Path(f).is_dir()]
        if missing:
            self._append_log("folder does not exist: " + ", ".join(missing))
            return False

        def job(conn, stop):
            report = _ScanReport()
            for folder in folders:
                if stop():
                    break
                summary = scan_library(conn, folder, should_stop=stop)
                report.parts.append(summary.format())
            return report

        self._start("rescan", job)
        return True

    def run(self) -> None:
        """Run: hand the ticked steps to the window, which runs them in order."""
        plan = self.plan()
        if plan.empty:
            self._append_log("tick at least one step: Attributes, Map layout or Ranking")
            return
        if self.running:
            self._append_log("a job is already running")
            return
        self.save_settings()
        self.run_requested.emit(plan)

    def run_attributes(self) -> bool:
        """The Attributes step: analysis, segmentation, embedding over the
        scope. False (with a log line) if nothing started."""
        try:
            settings = self.collect_settings()
        except ValueError as exc:
            self._append_log(f"settings: {exc}")
            return False
        if not settings.scope:
            self._append_log(
                "nothing in scope: tick a folder (or add one) before recomputing — "
                "nothing is implicit (§9.6)"
            )
            return False
        self.save_settings()
        encoder = self._encoder_factory(settings.embedding) if self._encoder_factory else None
        self._start(
            "recompute attributes",
            lambda conn, stop: recompute_attributes(
                conn, settings, should_stop=stop, encoder=encoder
            ),
        )
        return True

    def stop(self) -> None:
        if self._thread is None:
            return
        self._thread.request_stop()
        self._stop_button.setEnabled(False)
        self._append_log("stop requested: finishing the current file; later steps are dropped")

    def shutdown(self) -> None:
        """Last resort for a caller tearing the panel down while a job runs:
        ask it to stop and wait — unbounded, because the stop lands after the
        current file and a thread destroyed mid-run takes the process down.
        The window itself defers its close instead (`MainWindow.closeEvent`)."""
        thread = self._thread
        if thread is not None:
            thread.request_stop()
            thread.wait()
            logging.getLogger("crate").removeHandler(self._handler)
        self._conn.close()

    def start_job(self, name: str, job: Job) -> None:
        """Run `job(conn, should_stop)` on the worker — the window's map-layout
        step comes through here so every job shares the log, Stop and reload."""
        self._start(name, job)

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
        name, completed = "", False
        if thread is not None:
            name, completed = thread.name, thread.completed
            thread.deleteLater()
        self._set_running(False)
        self.refresh_folders()
        self.index_changed.emit()
        self.job_ended.emit(name, completed)

    def _set_running(self, running: bool) -> None:
        for widget in (
            self._rescan_button, self._add_button, self._remove_button, self._run_button, self._folders,
        ):
            widget.setEnabled(not running)
        self._stop_button.setEnabled(running)

    def _append_log(self, text: str) -> None:
        self._log.appendPlainText(text)

    def _on_folder_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """A tick in the tree. The tree is adjusted in place — never rebuilt
        from inside its own change signal."""
        if self._refreshing:
            return
        path = item.data(_COL_PATH, Qt.ItemDataRole.UserRole)
        checked = item.checkState(column) == Qt.CheckState.Checked
        if column == _COL_ROOT:
            set_root(self._conn, path if checked else None)
            self._refreshing = True
            try:
                for i in range(self._folders.topLevelItemCount()):
                    other = self._folders.topLevelItem(i)
                    if other is not item:
                        other.setCheckState(_COL_ROOT, Qt.CheckState.Unchecked)
            finally:
                self._refreshing = False
        elif column == _COL_SCOPE:
            set_in_scope(self._conn, path, checked)
            self._refreshing = True
            try:
                self._style_scope(item, checked)
            finally:
                self._refreshing = False
            self.scope_changed.emit()

    def _style_scope(self, item: QTreeWidgetItem, in_scope: bool) -> None:
        role = self.palette().ColorRole.Text if in_scope else self.palette().ColorRole.PlaceholderText
        item.setForeground(_COL_PATH, self.palette().brush(role))

    def _on_add_folder(self) -> None:
        start = self.library_root or (DEFAULT_LIBRARY_PATH if Path(DEFAULT_LIBRARY_PATH).is_dir() else "")
        chosen = QFileDialog.getExistingDirectory(self, "Add a folder to the library", start)
        if chosen:
            self.add_folder(chosen)

    def _on_remove_folder(self) -> None:
        self.remove_folder(None, confirm=True)


def _step(box: QCheckBox, description: str, options: list[QRadioButton]) -> QVBoxLayout:
    """One Recompute step: its tick box, a one-line explanation, its options."""
    column = QVBoxLayout()
    column.setSpacing(0)
    column.addWidget(box)
    text = QLabel(description)
    text.setWordWrap(True)
    text.setObjectName("caption")
    text.setContentsMargins(24, 0, 0, 2)
    column.addWidget(text)
    row = QHBoxLayout()
    row.setContentsMargins(24, 0, 0, 8)
    row.setSpacing(16)
    for option in options:
        row.addWidget(option)
    row.addStretch(1)
    column.addLayout(row)
    return column


def _pair(left: QWidget, right: QWidget) -> QWidget:
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(left)
    row.addWidget(right, stretch=1)
    return box

