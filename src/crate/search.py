"""The Search tab (2026-09-08, the user's steer: the Attributes tab was
crammed, so everything that narrows or searches the list moved here).

The CLAP text search box (§5.2), the filters that have no column of
their own (§9.5): minimum CLAP scores and — once there is an anchor — the
per-axis distance ranges; and the weight bars (the ranking's blend, §5.1),
here since 2026-09-08 because they shape what a search and a ranking mean. Type, length and tempo are filtered from the
list's own header since later that day (`headerfilter.py`). The panel owns
no data: it emits what the user asked for and the window applies it.
Filters are view state, never persisted (§9.4).
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .attributes import AXIS_HELP, CLASS_HELP, _prompts_text
from .catalog import Criteria
from .embedding import CONTENT_CLASSES
from .similarity import AXES, AXIS_LABELS
from .theme import SqueezableWidget

_KEY_WEIGHT = "weights/"


class SearchPanel(QWidget):
    search_requested = Signal(str)
    search_cleared = Signal()
    criteria_changed = Signal(object)   # a Criteria
    weights_changed = Signal(object)    # {axis: 0..1}

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

        # (2) filters without a column of their own — type, length and tempo
        # are the list header's (2026-09-08)
        filters_group = QGroupBox("Filters")
        filters_layout = QVBoxLayout(filters_group)
        note = QLabel("Type, length, tempo, key, tags and the scores filter from the list's column headers.")
        note.setWordWrap(True)
        note.setObjectName("caption")
        filters_layout.addWidget(note)
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
        self._ranges_group.setToolTip("Anchor a row (the circle at its start) to unlock these: a hard cutoff per axis.")
        filters_layout.addWidget(self._ranges_group)

        # (3) the weights (2026-09-08, the user's steer: they belong with the search)
        weights_group = QGroupBox("Weights — the list re-ranks as you move them")
        weights_group.setToolTip(
            "How much each axis counts in the ranking against the anchor — the anchored list "
            "re-ranks when a bar moves (a 25 ms pass, §9.6) — and in the map layout on its next "
            "Run. They are not the differences above."
        )
        weights_layout = QGridLayout(weights_group)
        self._weight_sliders: dict[str, QSlider] = {}
        self._weight_values: dict[str, QLabel] = {}
        for row, axis in enumerate(AXES):
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(int(settings.value(_KEY_WEIGHT + axis, 100, type=int)))
            slider.setToolTip(AXIS_HELP[axis])
            slider.setMinimumWidth(60)
            value = QLabel(f"{slider.value()} %")
            value.setMinimumWidth(40)
            slider.valueChanged.connect(lambda v, a=axis, lbl=value: self._on_weight(a, v, lbl))
            label = QLabel(AXIS_LABELS[axis])
            label.setToolTip(AXIS_HELP[axis])
            weights_layout.addWidget(label, row, 0)
            weights_layout.addWidget(slider, row, 1)
            weights_layout.addWidget(value, row, 2)
            self._weight_sliders[axis] = slider
            self._weight_values[axis] = value

        controls = SqueezableWidget()
        self._controls_layout = QVBoxLayout(controls)
        self._controls_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._controls_layout.addWidget(search_group)
        self._controls_layout.addWidget(filters_group)
        self._controls_layout.addWidget(weights_group)
        self._controls_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(controls)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(scroll)

    # --- widgets ---

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
        return Criteria(clap_min=clap_min, axis_ranges=tuple(ranges))

    def weights(self) -> dict[str, float]:
        return {axis: slider.value() / 100.0 for axis, slider in self._weight_sliders.items()}

    def search_text(self) -> str:
        return self._search.text().strip()

    # --- state in ---

    def set_anchor_state(self, has_anchor: bool) -> None:
        self._ranges_group.setEnabled(has_anchor)
        self._emit_criteria()

    def search_for(self, text: str) -> None:
        """Put `text` in the box and search — a chip on the Attributes tab or
        a bar in the header comes through here, so the box shows what was
        searched."""
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

    def _on_weight(self, axis: str, value: int, label: QLabel) -> None:
        label.setText(f"{value} %")
        self._settings.setValue(_KEY_WEIGHT + axis, value)
        self.weights_changed.emit(self.weights())
