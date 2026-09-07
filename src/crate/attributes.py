"""The Attributes tab (spec §9.5, Phase 7): merged filters and comparing-
factor weights, top to bottom exactly as the spec lists them.

(1) Weight bars per axis — the blend used on the *next* Recompute ranking,
never applied live (§9.6). (2) Free-text search over CLAP — instant.
(3) Filter criteria — content class / structural type, absolute duration
and tempo ranges, and per-axis distance-from-anchor ranges that unlock once
there is an anchor. Weight (blend importance) and range (hard cutoff) are
deliberately separate controls on the same axis. Below those, the selected
sample's auto-tag chips (§5.2): click one to search for it; editing chips
is Phase 11.

The panel owns no data: it emits what the user asked for and the window
applies it. Weights persist (§9.4); filters are view state.
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
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .catalog import Criteria
from .similarity import AXES, AXIS_LABELS

_KEY_WEIGHT = "weights/"

CLASS_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Rhythmic", "rhythmic"),
    ("Melodic", "melodic"),
    ("Vocal", "vocal"),
    ("Other", "other"),
    ("Unclassified", ""),
)
TYPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("One-shot", "one-shot"),
    ("Multi-hit", "multi-hit"),
    ("Loop", "loop"),
)


class AttributesPanel(QWidget):
    search_requested = Signal(str)
    search_cleared = Signal()
    criteria_changed = Signal(object)   # a Criteria
    weights_changed = Signal(object)    # {axis: 0..1}

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings

        # (1) comparing-factor weights
        weights_group = QGroupBox("Comparing factors — blend for the next ranking")
        weights_layout = QGridLayout(weights_group)
        self._weight_sliders: dict[str, QSlider] = {}
        self._weight_values: dict[str, QLabel] = {}
        for row, axis in enumerate(AXES):
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(int(settings.value(_KEY_WEIGHT + axis, 100, type=int)))
            value = QLabel(f"{slider.value()} %")
            value.setMinimumWidth(40)
            slider.valueChanged.connect(lambda v, a=axis, lbl=value: self._on_weight(a, v, lbl))
            weights_layout.addWidget(QLabel(AXIS_LABELS[axis]), row, 0)
            weights_layout.addWidget(slider, row, 1)
            weights_layout.addWidget(value, row, 2)
            self._weight_sliders[axis] = slider
            self._weight_values[axis] = value

        # (2) free-text search
        search_group = QGroupBox("Search")
        search_layout = QVBoxLayout(search_group)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Describe a sound… (CLAP text search)")
        self._search.setClearButtonEnabled(True)
        self._search.returnPressed.connect(self._emit_search)
        self._search_button = QPushButton("Search")
        self._search_button.clicked.connect(self._emit_search)
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._clear_search)
        search_row = QHBoxLayout()
        search_row.addWidget(self._search, stretch=1)
        search_row.addWidget(self._search_button)
        search_row.addWidget(clear_button)
        search_layout.addLayout(search_row)
        self._tags_label = QLabel("Tags of the selected sample:")
        self._tags_row = QHBoxLayout()
        self._tags_row.addStretch(1)
        self._tag_buttons: list[QPushButton] = []
        search_layout.addWidget(self._tags_label)
        search_layout.addLayout(self._tags_row)

        # (3) filters
        filters_group = QGroupBox("Filters")
        filters_layout = QVBoxLayout(filters_group)
        self._class_boxes: dict[str, QCheckBox] = {}
        class_row = QHBoxLayout()
        class_row.addWidget(QLabel("Class:"))
        for label, key in CLASS_OPTIONS:
            box = QCheckBox(label)
            box.setChecked(True)
            box.toggled.connect(self._emit_criteria)
            class_row.addWidget(box)
            self._class_boxes[key] = box
        class_row.addStretch(1)
        self._type_boxes: dict[str, QCheckBox] = {}
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Type:"))
        for label, key in TYPE_OPTIONS:
            box = QCheckBox(label)
            box.setChecked(True)
            box.toggled.connect(self._emit_criteria)
            type_row.addWidget(box)
            self._type_boxes[key] = box
        type_row.addStretch(1)
        filters_layout.addLayout(class_row)
        filters_layout.addLayout(type_row)

        absolute = QFormLayout()
        self._duration_min = self._seconds_box()
        self._duration_max = self._seconds_box()
        absolute.addRow("Length (s), 0 = any", _pair(self._duration_min, self._duration_max))
        self._tempo_min = self._bpm_box()
        self._tempo_max = self._bpm_box()
        absolute.addRow("Tempo (BPM), 0 = any", _pair(self._tempo_min, self._tempo_max))
        filters_layout.addLayout(absolute)

        self._ranges_group = QGroupBox("Distance from the anchor, % of the library's spread")
        ranges_layout = QGridLayout(self._ranges_group)
        self._range_min: dict[str, QSpinBox] = {}
        self._range_max: dict[str, QSpinBox] = {}
        for row, axis in enumerate(AXES):
            low = self._percent_box(0)
            high = self._percent_box(100)
            ranges_layout.addWidget(QLabel(AXIS_LABELS[axis]), row, 0)
            ranges_layout.addWidget(low, row, 1)
            ranges_layout.addWidget(QLabel("to"), row, 2)
            ranges_layout.addWidget(high, row, 3)
            self._range_min[axis] = low
            self._range_max[axis] = high
        self._ranges_group.setEnabled(False)
        self._ranges_group.setToolTip("Pin an anchor (⚓) to unlock these.")
        filters_layout.addWidget(self._ranges_group)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.addWidget(weights_group)
        controls_layout.addWidget(search_group)
        controls_layout.addWidget(filters_group)
        controls_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(controls)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(scroll)

    # --- widgets ---

    def _seconds_box(self) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0.0, 100_000.0)
        box.setDecimals(2)
        box.setSingleStep(0.1)
        box.setSpecialValueText("any")
        box.valueChanged.connect(self._emit_criteria)
        return box

    def _bpm_box(self) -> QSpinBox:
        box = QSpinBox()
        box.setRange(0, 1000)
        box.setSpecialValueText("any")
        box.valueChanged.connect(self._emit_criteria)
        return box

    def _percent_box(self, value: int) -> QSpinBox:
        box = QSpinBox()
        box.setRange(0, 100)
        box.setSuffix(" %")
        box.setValue(value)
        box.valueChanged.connect(self._emit_criteria)
        return box

    # --- state out ---

    def weights(self) -> dict[str, float]:
        return {axis: slider.value() / 100.0 for axis, slider in self._weight_sliders.items()}

    def criteria(self) -> Criteria:
        classes = frozenset(k for k, box in self._class_boxes.items() if box.isChecked())
        types = frozenset(k for k, box in self._type_boxes.items() if box.isChecked())
        ranges: list[tuple[str, float, float]] = []
        if self._ranges_group.isEnabled():
            for axis in AXES:
                low = self._range_min[axis].value()
                high = self._range_max[axis].value()
                if (low, high) != (0, 100):
                    ranges.append((axis, low / 100.0, high / 100.0))
        return Criteria(
            classes=None if len(classes) == len(self._class_boxes) else classes,
            types=None if len(types) == len(self._type_boxes) else types,
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

    def show_tags(self, tags: list[tuple[str, float]]) -> None:
        for button in self._tag_buttons:
            self._tags_row.removeWidget(button)
            button.deleteLater()
        self._tag_buttons = []
        for tag, score in tags:
            button = QPushButton(f"{tag}  {score * 100:.0f}")
            button.setFlat(True)
            button.setToolTip(f"search for “{tag}”")
            button.clicked.connect(lambda _checked=False, t=tag: self.search_for(t))
            self._tags_row.insertWidget(len(self._tag_buttons), button)
            self._tag_buttons.append(button)

    def search_for(self, text: str) -> None:
        self._search.setText(text)
        self._emit_search()

    # --- plumbing ---

    def _on_weight(self, axis: str, value: int, label: QLabel) -> None:
        label.setText(f"{value} %")
        self._settings.setValue(_KEY_WEIGHT + axis, value)
        self.weights_changed.emit(self.weights())

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
