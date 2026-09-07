"""The Attributes tab (spec §9.5, Phase 7; reshaped 2026-09-07 on the
user's steer).

What the user wants to see first is how the **selected** sample differs
from the **anchor**, axis by axis — so that is the top block: read-only
bars, in % of the library's spread, with a tooltip on every axis saying what
it measures. Then the CLAP search with the selected sample's chips, the
selected sample's **segments** (moved here from the bottom panel, which now
shows the waveform), the filters, and — last, because they only matter when
you press Recompute — the **weights** for the next ranking or map layout.
Weight (blend importance) and range (hard cutoff) stay separate controls on
the same axis.

The panel owns no data: it emits what the user asked for and the window
applies it. Weights persist (§9.4); filters are view state.
"""

from __future__ import annotations

from collections.abc import Mapping

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
    QProgressBar,
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

AXIS_HELP: dict[str, str] = {
    "amplitude": "Level and envelope: peak, RMS, crest factor, attack and decay times.",
    "pitch": "Fundamental frequency. Only material that passes the harmonic gate has "
             "a pitch — unpitched sounds have no pitch axis at all.",
    "timbre": "MFCC means and variances plus spectral contrast: the "
              "\"same instrument / same material\" descriptor set.",
    "spectrum": "Brightness and noisiness independent of pitch: spectral centroid, "
                "bandwidth, rolloff and flatness.",
    "conceptual": "The CLAP embedding: \"sounds alike\" even when the descriptors "
                  "disagree. Also the space text search runs in.",
}
CLASS_HELP = (
    "Facet A of the taxonomy (spec §4): CLAP's zero-shot guess among Rhythmic "
    "(hits, foley, drum loops), Melodic (tonal), Vocal (speech, vocal chops) and "
    "Other (textures, drones, ambiences). Below 50 % confidence the sample is left "
    "unclassified. Measured 68–73 % right on drum packs; on foley it is much weaker — "
    "a hint, not a fact."
)
TYPE_HELP = (
    "Facet B (spec §4), from the audio itself: one-shot = one dominant onset (up to the "
    "one-shot max duration), loop = a whole number of beats at a detectable tempo, "
    "multi-hit = everything else with several transients."
)

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

        # (1) selected vs anchor — the per-axis difference
        diff_group = QGroupBox("Selected vs anchor — difference per axis")
        diff_layout = QGridLayout(diff_group)
        self._diff_caption = QLabel("pin an anchor (⚓) and select a sample or a hit to compare")
        self._diff_caption.setWordWrap(True)
        diff_layout.addWidget(self._diff_caption, 0, 0, 1, 2)
        self._diff_bars: dict[str, QProgressBar] = {}
        for row, axis in enumerate(AXES, start=1):
            label = QLabel(AXIS_LABELS[axis])
            label.setToolTip(AXIS_HELP[axis])
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(True)
            bar.setToolTip(
                AXIS_HELP[axis] + "\n\n0 % = identical on this axis, 100 % = as far apart as "
                "the 95th percentile of the library."
            )
            diff_layout.addWidget(label, row, 0)
            diff_layout.addWidget(bar, row, 1)
            self._diff_bars[axis] = bar
        diff_layout.setColumnStretch(1, 1)
        self.show_difference(None, None, None)

        # (2) free-text search + the selected sample's chips
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
        self._tags_label = QLabel("Tags of the selected sample (CLAP zero-shot; click to search):")
        self._tags_row = QHBoxLayout()
        self._tags_row.addStretch(1)
        self._tag_buttons: list[QPushButton] = []
        search_layout.addWidget(self._tags_label)
        search_layout.addLayout(self._tags_row)

        # (3) the selected sample's segments — hosted for the window (§6.4 drill-down)
        self._segments_group = QGroupBox("Segments of the selected sample")
        self._segments_layout = QVBoxLayout(self._segments_group)
        self._segments_group.setToolTip(
            "Every hit found inside the selected sample: start, length, transient strength, "
            "auto or manual. Select one to preview it; drag one into Bitwig."
        )

        # (4) filters
        filters_group = QGroupBox("Filters")
        filters_layout = QVBoxLayout(filters_group)
        self._type_boxes: dict[str, QCheckBox] = {}
        type_row = QHBoxLayout()
        type_label = QLabel("Type:")
        type_label.setToolTip(TYPE_HELP)
        type_row.addWidget(type_label)
        for label, key in TYPE_OPTIONS:
            box = QCheckBox(label)
            box.setChecked(True)
            box.setToolTip(TYPE_HELP)
            box.toggled.connect(self._emit_criteria)
            type_row.addWidget(box)
            self._type_boxes[key] = box
        type_row.addStretch(1)
        self._class_boxes: dict[str, QCheckBox] = {}
        class_row = QHBoxLayout()
        class_label = QLabel("CLAP class guess:")
        class_label.setToolTip(CLASS_HELP)
        class_row.addWidget(class_label)
        for label, key in CLASS_OPTIONS:
            box = QCheckBox(label)
            box.setChecked(True)
            box.setToolTip(CLASS_HELP)
            box.toggled.connect(self._emit_criteria)
            class_row.addWidget(box)
            self._class_boxes[key] = box
        class_row.addStretch(1)
        filters_layout.addLayout(type_row)
        filters_layout.addLayout(class_row)

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
            label = QLabel(AXIS_LABELS[axis])
            label.setToolTip(AXIS_HELP[axis])
            ranges_layout.addWidget(label, row, 0)
            ranges_layout.addWidget(low, row, 1)
            ranges_layout.addWidget(QLabel("to"), row, 2)
            ranges_layout.addWidget(high, row, 3)
            self._range_min[axis] = low
            self._range_max[axis] = high
        self._ranges_group.setEnabled(False)
        self._ranges_group.setToolTip("Pin an anchor (⚓) to unlock these: a hard cutoff per axis.")
        filters_layout.addWidget(self._ranges_group)

        # (5) weights — for the next Recompute ranking / map layout only
        weights_group = QGroupBox("Weights for the next ranking and map layout")
        weights_group.setToolTip(
            "How much each axis counts when you press Recompute ranking or Recompute map "
            "layout. Moving these changes nothing until you do (§9.6). They are not the "
            "differences above."
        )
        weights_layout = QGridLayout(weights_group)
        self._weight_sliders: dict[str, QSlider] = {}
        self._weight_values: dict[str, QLabel] = {}
        for row, axis in enumerate(AXES):
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(int(settings.value(_KEY_WEIGHT + axis, 100, type=int)))
            slider.setToolTip(AXIS_HELP[axis])
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

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.addWidget(diff_group)
        controls_layout.addWidget(search_group)
        controls_layout.addWidget(self._segments_group)
        controls_layout.addWidget(filters_group)
        controls_layout.addWidget(weights_group)
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

    def host_segments(self, widget: QWidget) -> None:
        """The window's segments table lives in this tab (§6.4 drill-down)."""
        widget.setMinimumHeight(120)
        self._segments_layout.addWidget(widget)

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

    def difference_values(self) -> dict[str, int | None]:
        """What the difference bars show (tests read this)."""
        return {
            axis: (None if bar.format() == "n/a" else bar.value())
            for axis, bar in self._diff_bars.items()
        }

    # --- state in ---

    def set_anchor_state(self, has_anchor: bool) -> None:
        self._ranges_group.setEnabled(has_anchor)
        self._emit_criteria()

    def show_difference(
        self,
        anchor_label: str | None,
        selected_label: str | None,
        distances: Mapping[str, float] | None,
    ) -> None:
        """The selected item's per-axis distance from the anchor, as bars.
        `distances` in 0..1 (NaN / missing = the item lacks that axis)."""
        if anchor_label is None:
            self._diff_caption.setText("pin an anchor (⚓) and select a sample or a hit to compare")
        elif selected_label is None:
            self._diff_caption.setText(f"anchor: {anchor_label} — select a sample or a hit to compare")
        else:
            self._diff_caption.setText(f"anchor: {anchor_label}\nselected: {selected_label}")
        for axis, bar in self._diff_bars.items():
            value = None if distances is None else distances.get(axis)
            if value is None or value != value:
                bar.setValue(0)
                bar.setFormat("n/a")
            else:
                bar.setValue(int(round(min(max(value, 0.0), 1.0) * 100)))
                bar.setFormat("%v %")

    def show_tags(self, tags: list[tuple[str, float]]) -> None:
        for button in self._tag_buttons:
            self._tags_row.removeWidget(button)
            button.hide()                  # gone now, not at the next event-loop turn
            button.setParent(None)
            button.deleteLater()
        self._tag_buttons = []
        for tag, score in tags:
            button = QPushButton(f"{tag}  {score * 100:.0f}")
            button.setFlat(True)
            button.setToolTip(f"CLAP zero-shot chip, cosine {score:.2f} — click to search for “{tag}”")
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
