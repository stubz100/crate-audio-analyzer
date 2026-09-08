"""The Search tab (2026-09-08, the user's steer: the Attributes tab was
crammed, so everything that narrows or searches the list moved here).

The CLAP text search box (§5.2) and the filters (§9.5): structural type,
minimum CLAP scores, length and tempo, and — once there is an anchor — the
per-axis distance ranges. The panel owns no data: it emits what the user
asked for and the window applies it. Filters are view state, never
persisted (§9.4).
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .attributes import AXIS_HELP, CLASS_HELP, _prompts_text
from .catalog import Criteria
from .embedding import CONTENT_CLASSES
from .similarity import AXES, AXIS_LABELS
from .theme import SqueezableWidget

TYPE_HELP = (
    "Facet B (spec §4), from the audio itself: one-shot = one dominant onset (up to the "
    "one-shot max duration), loop = a whole number of beats at a detectable tempo, "
    "multi-hit = everything else with several transients."
)

TYPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("One-shot", "one-shot"),
    ("Multi-hit", "multi-hit"),
    ("Loop", "loop"),
)


class SearchPanel(QWidget):
    search_requested = Signal(str)
    search_cleared = Signal()
    criteria_changed = Signal(object)   # a Criteria

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings

        # (1) free-text search
        search_group = QGroupBox("Search")
        search_layout = QVBoxLayout(search_group)
        hint = QLabel(
            "Describe a sound; the list shows the best matches first, and a hit inside a "
            "longer file shows as its own row. The first search loads the model (a few seconds)."
        )
        hint.setWordWrap(True)
        hint.setObjectName("caption")
        search_layout.addWidget(hint)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Describe a sound… (CLAP text search)")
        self._search.setClearButtonEnabled(True)
        self._search.returnPressed.connect(self._emit_search)
        self._search_button = QPushButton("Search")
        self._search_button.clicked.connect(self._emit_search)
        clear_button = QPushButton("Clear")
        clear_button.setToolTip("Drop the search: the list goes back to its plain order (or the ranking).")
        clear_button.clicked.connect(self._clear_search)
        search_row = QHBoxLayout()
        search_row.addWidget(self._search, stretch=1)
        search_row.addWidget(self._search_button)
        search_row.addWidget(clear_button)
        search_layout.addLayout(search_row)

        # (2) filters — they narrow whatever the list shows: plain, searched or ranked
        filters_group = QGroupBox("Filters")
        filters_layout = QVBoxLayout(filters_group)
        self._type_boxes: dict[str, QCheckBox] = {}
        filters_layout.addLayout(self._checkbox_grid("Type", TYPE_HELP, TYPE_OPTIONS, self._type_boxes))
        self._clap_min: dict[str, QSpinBox] = {}
        clap_row = QGridLayout()
        clap_label = QLabel("CLAP score at least:")
        clap_label.setToolTip(CLASS_HELP)
        clap_row.addWidget(clap_label, 0, 0, 1, 4)
        for n, name in enumerate(CONTENT_CLASSES):
            box = self._percent_box(0)
            box.setSpecialValueText("any")
            box.setToolTip(_prompts_text(name))
            clap_row.addWidget(QLabel(name.capitalize()), 1 + n // 2, 2 * (n % 2))
            clap_row.addWidget(box, 1 + n // 2, 1 + 2 * (n % 2))
            self._clap_min[name] = box
        clap_row.setColumnStretch(4, 1)
        filters_layout.addLayout(clap_row)

        absolute = QFormLayout()
        absolute.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self._duration_min = self._seconds_box()
        self._duration_max = self._seconds_box()
        absolute.addRow("Length (s)", _pair(self._duration_min, self._duration_max))
        self._tempo_min = self._bpm_box()
        self._tempo_max = self._bpm_box()
        absolute.addRow("Tempo (BPM)", _pair(self._tempo_min, self._tempo_max))
        filters_layout.addLayout(absolute)

        self._ranges_group = QGroupBox("Distance from the anchor (% of spread)")
        ranges_layout = QGridLayout(self._ranges_group)
        self._range_min: dict[str, QSpinBox] = {}
        self._range_max: dict[str, QSpinBox] = {}
        for row, axis in enumerate(AXES):
            low = self._percent_box(0)
            high = self._percent_box(100)
            label = QLabel(AXIS_LABELS[axis])
            label.setToolTip(AXIS_HELP[axis])
            ranges_layout.addWidget(label, row, 0)
            ranges_layout.addWidget(low, row, 1)
            ranges_layout.addWidget(QLabel("to"), row, 2)
            ranges_layout.addWidget(high, row, 3)
            self._range_min[axis] = low
            self._range_max[axis] = high
        ranges_layout.setColumnStretch(4, 1)
        self._ranges_group.setEnabled(False)
        self._ranges_group.setToolTip("Press ⚓ on a row to unlock these: a hard cutoff per axis.")
        filters_layout.addWidget(self._ranges_group)

        controls = SqueezableWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        controls_layout.addWidget(search_group)
        controls_layout.addWidget(filters_group)
        controls_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(controls)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(scroll)

    # --- widgets ---

    def _checkbox_grid(self, title: str, help_text: str, options, boxes: dict, per_row: int = 3) -> QGridLayout:
        """A labelled row of checkboxes that wraps — a single row of five
        was what pushed the panel past its pane (2026-09-07)."""
        grid = QGridLayout()
        label = QLabel(title + ":")
        label.setToolTip(help_text)
        grid.addWidget(label, 0, 0)
        for n, (text, key) in enumerate(options):
            box = QCheckBox(text)
            box.setChecked(True)
            box.setToolTip(help_text)
            box.toggled.connect(self._emit_criteria)
            grid.addWidget(box, n // per_row, 1 + n % per_row)
            boxes[key] = box
        grid.setColumnStretch(per_row + 1, 1)
        return grid

    def _seconds_box(self) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0.0, 9_999.99)
        box.setDecimals(2)
        box.setSingleStep(0.1)
        box.setSpecialValueText("any")
        box.setMaximumWidth(84)
        box.valueChanged.connect(self._emit_criteria)
        return box

    def _bpm_box(self) -> QSpinBox:
        box = QSpinBox()
        box.setRange(0, 1000)
        box.setSpecialValueText("any")
        box.setMaximumWidth(72)
        box.valueChanged.connect(self._emit_criteria)
        return box

    def _percent_box(self, value: int) -> QSpinBox:
        box = QSpinBox()
        box.setRange(0, 100)
        box.setSuffix(" %")
        box.setValue(value)
        box.setMaximumWidth(68)
        box.valueChanged.connect(self._emit_criteria)
        return box

    # --- state out ---

    def criteria(self) -> Criteria:
        types = frozenset(k for k, box in self._type_boxes.items() if box.isChecked())
        clap_min = tuple(
            (name, box.value() / 100.0) for name, box in self._clap_min.items() if box.value() > 0
        )
        ranges: list[tuple[str, float, float]] = []
        if self._ranges_group.isEnabled():
            for axis in AXES:
                low = self._range_min[axis].value()
                high = self._range_max[axis].value()
                if (low, high) != (0, 100):
                    ranges.append((axis, low / 100.0, high / 100.0))
        return Criteria(
            types=None if len(types) == len(self._type_boxes) else types,
            clap_min=clap_min,
            duration_s=(
                self._duration_min.value() or None,
                self._duration_max.value() or None,
            ),
            tempo_bpm=(
                float(self._tempo_min.value()) or None,
                float(self._tempo_max.value()) or None,
            ),
            axis_ranges=tuple(ranges),
        )

    def search_text(self) -> str:
        return self._search.text().strip()

    # --- state in ---

    def set_anchor_state(self, has_anchor: bool) -> None:
        self._ranges_group.setEnabled(has_anchor)
        self._emit_criteria()

    def search_for(self, text: str) -> None:
        """Put `text` in the box and search — a chip on the Attributes tab
        comes through here, so the box shows what was searched."""
        self._search.setText(text)
        self._emit_search()

    # --- plumbing ---

    def _emit_search(self) -> None:
        text = self.search_text()
        if text:
            self.search_requested.emit(text)

    def _clear_search(self) -> None:
        self._search.clear()
        self.search_cleared.emit()

    def _emit_criteria(self, *_args) -> None:
        self.criteria_changed.emit(self.criteria())


def _pair(left: QWidget, right: QWidget) -> QWidget:
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(left)
    row.addWidget(QLabel("to"))
    row.addWidget(right)
    row.addStretch(1)
    return box
