"""The Attributes tab (spec §9.5, Phase 7; reshaped 2026-09-07 and again
2026-09-08 on the user's steer): everything about the **selected** sample.

How it differs from the **anchor**, axis by axis — read-only bars in % of
the library's spread, a tooltip on every axis saying what it measures.
Everything else moved out on the user's steer (2026-09-08): the search box,
the filters and the weight bars to the Search tab (`search.py`); the tag
chips, the tag-score bars and the embedding stripes to the window's header
(`tagbars.py`, `vectorstrip.py`); the caption to the top of the waveform
panel; the segments table went — the waveform shows them.

The panel owns no data: it emits what the user asked for and the window
applies it.
"""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QLabel,
    QLayout,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .embedding import CLASS_PROMPTS
from .similarity import AXES, AXIS_LABELS
from .theme import SqueezableWidget


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


class AttributesPanel(QWidget):

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings

        # (1) selected vs anchor — the per-axis difference
        diff_group = QGroupBox("Selected vs anchor — difference per axis")
        diff_layout = QGridLayout(diff_group)
        self._diff_caption = QLabel("anchor a row (the circle at its start) and select a sample or a hit to compare")
        self._diff_caption.setWordWrap(True)
        diff_layout.addWidget(self._diff_caption, 0, 0, 1, 2)
        self._diff_bars: dict[str, QProgressBar] = {}
        for row, axis in enumerate(AXES, start=1):
            label = QLabel(AXIS_LABELS[axis])
            label.setToolTip(AXIS_HELP[axis])
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(True)
            bar.setMinimumWidth(60)
            bar.setToolTip(
                AXIS_HELP[axis] + "\n\n0 % = identical on this axis, 100 % = as far apart as "
                "the 95th percentile of the library."
            )
            diff_layout.addWidget(label, row, 0)
            diff_layout.addWidget(bar, row, 1)
            self._diff_bars[axis] = bar
        diff_layout.setColumnStretch(1, 1)
        self.show_difference(None, None, None)

        controls = SqueezableWidget()
        controls_layout = QVBoxLayout(controls)
        # Let the contents squeeze to the pane instead of enforcing their
        # minimum: a five-checkbox row must never push the panel past its edge.
        controls_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        controls_layout.addWidget(diff_group)
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

    # --- state out ---

    def difference_values(self) -> dict[str, int | None]:
        """What the difference bars show (tests read this)."""
        return {
            axis: (None if bar.format() == "n/a" else bar.value())
            for axis, bar in self._diff_bars.items()
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
            if value is None or value != value:
                bar.setValue(0)
                bar.setFormat("n/a")
            else:
                bar.setValue(int(round(min(max(value, 0.0), 1.0) * 100)))
                bar.setFormat("%v %")
