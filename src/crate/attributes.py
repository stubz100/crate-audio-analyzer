"""The Attributes tab (spec §9.5, Phase 7; reshaped 2026-09-07 and again
2026-09-08 on the user's steer): everything about the **selected** sample.

How it differs from the **anchor**, axis by axis — read-only bars in % of
the library's spread, a tooltip on every axis saying what it measures.
Everything else moved out on the user's steer (2026-09-08): the search box,
the filters and the weight bars to the Search tab (`search.py`); the tag
chips, the tag-score bars and the embedding stripes to the window's header
(`tagbars.py`, `vectorstrip.py`); the caption to the top of the waveform
panel; the segments table went — the waveform shows them.

**Correct** (Phase 11, 2026-09-13; spec §4, §8, §11): the selected sample's
two facets as controls the user can overrule — the content class and the
structural type, each with what the machine said beside it and a way back
to the machine's call — and its curated tags: the ones it carries (a × takes
one off), a box to add one, and the machine's chips it does not carry yet (a
click promotes one). A correction is protected from the next recompute by
`corrections.py`; the panel only asks.

The panel owns no data: it emits what the user asked for and the window
applies it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QCompleter,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .corrections import CLASS_LABELS, CONTENT_CLASSES, STRUCTURAL_TYPES, TYPE_LABELS, Classification
from .embedding import CLASS_PROMPTS
from .similarity import AXES, AXIS_LABELS
from .design import TOKENS
from .theme import SqueezableWidget
from .widgets import FlowLayout, Meter, SectionHeader, SegmentedControl, TagChip, make_button


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
    "CLAP compares the sample's embedding with four sets of twelve text prompts. Each set "
    "scores as its best-matching prompt; the four scores are scaled by the model's logit "
    "scale and softmaxed into percentages that sum to 100. Spec §4 called the largest of "
    "them the sample's class; since 2026-09-08 the numbers feed only this filter."
)


def _prompts_text(name: str) -> str:
    return "Best of these prompts:\n" + "\n".join(f"• {p}" for p in CLASS_PROMPTS[name])


@dataclass(frozen=True)
class CorrectionState:
    """What the Correct section shows for the current sample (Phase 11)."""

    label: str                                   # the sample's file name
    classification: Classification
    user_tags: list[str] = field(default_factory=list)
    suggested: list[tuple[str, float]] = field(default_factory=list)   # the machine's chips not yet curated
    known_tags: list[str] = field(default_factory=list)                # every curated tag: the box's completions
    selected_count: int = 1                      # rows selected in the list: the "apply to all" offer


class AttributesPanel(QWidget):

    # --- Correct (Phase 11): what the user asked for; the window applies it ---
    class_chosen = Signal(str)          # a content class (`corrections.CONTENT_CLASSES`)
    type_chosen = Signal(str)           # a structural type (`corrections.STRUCTURAL_TYPES`)
    class_reset = Signal()              # back to CLAP's call
    type_reset = Signal()               # back to the rule's call
    tag_added = Signal(str)             # typed, or a suggestion promoted
    tag_removed = Signal(str)
    tag_searched = Signal(str)          # a curated chip clicked: search for it

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings

        # (1) selected vs anchor — the per-axis difference
        diff_group = QGroupBox("Selected vs anchor — difference per axis")
        diff_layout = QGridLayout(diff_group)
        self._diff_caption = QLabel("anchor a row (the circle at its start) and select a sample or a hit to compare")
        self._diff_caption.setWordWrap(True)
        diff_layout.addWidget(self._diff_caption, 0, 0, 1, 2)
        # One `Meter` per axis (2026-09-10, layer 2) instead of a label beside a
        # full-width QProgressBar reading "n/a": the meter carries its own label
        # and value, and an axis the item lacks shows its track and an em dash
        # rather than a wide empty box across the panel.
        self._diff_bars: dict[str, Meter] = {}
        for row, axis in enumerate(AXES, start=1):
            bar = Meter(AXIS_LABELS[axis], None, TOKENS.data.segment)
            bar.setToolTip(
                AXIS_HELP[axis] + "\n\n0 % = identical on this axis, 100 % = as far apart as "
                "the 95th percentile of the library."
            )
            diff_layout.addWidget(bar, row, 0, 1, 2)
            self._diff_bars[axis] = bar
        diff_layout.setColumnStretch(1, 1)
        self.show_difference(None, None, None)

        # (2) Correct — the selected sample's facets and curated tags (Phase 11)
        self._correct_group = QGroupBox("Correct the selected sample")
        self._correct_group.setToolTip(
            "What the machine decided, you can overrule; a recompute never takes it back (§11)."
        )
        correct = QVBoxLayout(self._correct_group)
        correct.setSpacing(TOKENS.metric.sm)
        self._correct_caption = QLabel("select a sample to correct it")
        self._correct_caption.setObjectName("caption")
        self._correct_caption.setWordWrap(True)
        correct.addWidget(self._correct_caption)

        self._class_control = SegmentedControl([(c, CLASS_LABELS[c]) for c in CONTENT_CLASSES])
        self._class_control.setToolTip("Facet A, the content class (§4). Your choice is protected from CLAP.")
        self._class_control.changed.connect(self.class_chosen)
        self._class_reset = make_button(icon_name="refresh", intent="quiet", tooltip="Back to CLAP's own call")
        self._class_reset.clicked.connect(self.class_reset)
        self._class_note = QLabel()
        self._class_note.setObjectName("caption")
        self._class_note.setWordWrap(True)
        correct.addWidget(SectionHeader("Class"))
        correct.addLayout(self._control_row(self._class_control, self._class_reset))
        correct.addWidget(self._class_note)

        self._type_control = SegmentedControl([(t, TYPE_LABELS[t]) for t in STRUCTURAL_TYPES])
        self._type_control.setToolTip(
            "Facet B, the structural type (§4). Your choice is protected from re-analysis, "
            "and the next Recompute segments the sample accordingly."
        )
        self._type_control.changed.connect(self.type_chosen)
        self._type_reset = make_button(icon_name="refresh", intent="quiet", tooltip="Back to the rule's call from the descriptors")
        self._type_reset.clicked.connect(self.type_reset)
        self._type_note = QLabel()
        self._type_note.setObjectName("caption")
        self._type_note.setWordWrap(True)
        correct.addWidget(SectionHeader("Type"))
        correct.addLayout(self._control_row(self._type_control, self._type_reset))
        correct.addWidget(self._type_note)

        correct.addWidget(SectionHeader("My tags"))
        self._tags_host = QWidget()
        self._tags_flow = FlowLayout(self._tags_host)
        correct.addWidget(self._tags_host)
        self._tag_box = QLineEdit()
        self._tag_box.setPlaceholderText("add a tag")
        self._tag_box.setToolTip("A tag of your own — Enter adds it; it is never touched by a recompute (§8).")
        self._tag_box.returnPressed.connect(self._submit_tag)
        self._tag_add = make_button(icon_name="chevron-right", intent="quiet", tooltip="Add the tag")
        self._tag_add.clicked.connect(self._submit_tag)
        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.addWidget(self._tag_box, stretch=1)
        add_row.addWidget(self._tag_add)
        correct.addLayout(add_row)

        correct.addWidget(SectionHeader("Suggested"))
        self._suggest_host = QWidget()
        self._suggest_flow = FlowLayout(self._suggest_host)
        correct.addWidget(self._suggest_host)
        self._suggest_note = QLabel("CLAP's chips this sample does not carry yet — a click adds one")
        self._suggest_note.setObjectName("caption")
        self._suggest_note.setWordWrap(True)
        correct.addWidget(self._suggest_note)

        self._apply_all = QCheckBox("Apply to all selected rows")
        self._apply_all.setToolTip(
            "With several rows selected in the list, a class, a type or a tag goes to every one of them."
        )
        self._apply_all.hide()
        correct.addWidget(self._apply_all)
        self.show_correction(None)

        controls = SqueezableWidget()
        controls_layout = QVBoxLayout(controls)
        # Let the contents squeeze to the pane instead of enforcing their
        # minimum: a five-checkbox row must never push the panel past its edge.
        controls_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        controls_layout.addWidget(diff_group)
        controls_layout.addWidget(self._correct_group)
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

    @staticmethod
    def _control_row(control: QWidget, reset: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TOKENS.metric.sm)
        row.addWidget(control, stretch=1)
        row.addWidget(reset)
        return row

    def _submit_tag(self) -> None:
        text = " ".join(self._tag_box.text().split())
        if text:
            self.tag_added.emit(text)
            self._tag_box.clear()

    # --- state out ---

    def difference_values(self) -> dict[str, int | None]:
        """What the difference bars show (tests read this)."""
        return {
            axis: (None if bar.value() is None else int(round(bar.value() * 100)))
            for axis, bar in self._diff_bars.items()
        }

    def apply_to_selection(self) -> bool:
        """Whether a correction goes to every selected row rather than the current sample."""
        return self._apply_all.isVisible() and self._apply_all.isChecked()

    def correction_values(self) -> dict:
        """What the Correct section shows (tests read this)."""
        return {
            "class": self._class_control.current(),
            "type": self._type_control.current(),
            "class_note": self._class_note.text(),
            "type_note": self._type_note.text(),
            "tags": [self._tags_flow.itemAt(i).widget().text() for i in range(self._tags_flow.count())],
            "suggested": [self._suggest_flow.itemAt(i).widget().text() for i in range(self._suggest_flow.count())],
            "enabled": self._correct_group.isEnabled(),
        }

    # --- state in ---

    def show_difference(
        self,
        anchor_label: str | None,
        selected_label: str | None,
        distances: Mapping[str, float] | None,
    ) -> None:
        """The selected item's per-axis distance from the anchor, as bars.
        `distances` in 0..1 (NaN / missing = the item lacks that axis)."""
        if anchor_label is None:
            self._diff_caption.setText("anchor a row (the circle at its start) and select a sample or a hit to compare")
        elif selected_label is None:
            self._diff_caption.setText(f"anchor: {anchor_label} — select a sample or a hit to compare")
        else:
            self._diff_caption.setText(f"anchor: {anchor_label}\nselected: {selected_label}")
        for axis, bar in self._diff_bars.items():
            value = None if distances is None else distances.get(axis)
            bar.set_value(None if value is None or value != value else min(max(value, 0.0), 1.0))

    def show_correction(self, state: CorrectionState | None) -> None:
        """The current sample's facets and tags; None when nothing is selected."""
        self._tags_flow.clear()
        self._suggest_flow.clear()
        if state is None:
            self._correct_group.setEnabled(False)
            self._correct_caption.setText("select a sample to correct it")
            self._set_quietly(self._class_control, None)
            self._set_quietly(self._type_control, None)
            self._class_note.clear()
            self._type_note.clear()
            self._class_reset.setEnabled(False)
            self._type_reset.setEnabled(False)
            self._apply_all.hide()
            return
        k = state.classification
        self._correct_group.setEnabled(True)
        self._correct_caption.setText(state.label)

        self._set_quietly(self._class_control, k.content_class)
        self._class_reset.setEnabled(k.content_class_confirmed)
        best = k.best_class
        said = f"{CLASS_LABELS.get(best, best)} {k.class_scores[best] * 100:.0f} %" if best else None
        if k.content_class_confirmed:
            self._class_note.setText(f"yours — CLAP said {said}" if said else "yours — CLAP has not scored this sample")
        elif k.content_class is not None:
            self._class_note.setText(f"CLAP's call: {said}" if said else "CLAP's call")
        elif said:
            self._class_note.setText(f"CLAP unsure — best guess {said}; pick one to settle it")
        else:
            self._class_note.setText("not classified yet — Recompute attributes embeds it")

        self._set_quietly(self._type_control, k.structural_type)
        self._type_reset.setEnabled(k.structural_type_confirmed)
        if k.structural_type_confirmed:
            self._type_note.setText("yours — the next Recompute segments the sample as this")
        elif k.structural_type is not None:
            self._type_note.setText("the rule's call from the descriptors (§4)")
        else:
            self._type_note.setText("not analysed yet — Recompute attributes types it")

        for name in state.user_tags:
            chip = TagChip(name, removable=True)
            chip.clicked.connect(self.tag_searched)
            chip.removed.connect(self.tag_removed)
            self._tags_flow.addWidget(chip)
        for name, score in state.suggested:
            chip = TagChip(name, suggested=True)
            chip.setToolTip(f"CLAP {score * 100:.0f} — click to add “{name}” to my tags")
            chip.clicked.connect(self.tag_added)
            self._suggest_flow.addWidget(chip)
        self._suggest_note.setVisible(bool(state.suggested))
        completer = QCompleter(state.known_tags)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._tag_box.setCompleter(completer)

        if state.selected_count > 1:
            self._apply_all.setText(f"Apply to all {state.selected_count} selected rows")
            self._apply_all.show()
        else:
            self._apply_all.hide()

    @staticmethod
    def _set_quietly(control: SegmentedControl, key: str | None) -> None:
        """Show a value without announcing it as a choice."""
        control.blockSignals(True)
        try:
            control.set_current(key)
        finally:
            control.blockSignals(False)
