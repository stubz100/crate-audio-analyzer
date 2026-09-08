"""The Attributes tab (spec §9.5, Phase 7; reshaped 2026-09-07 and again
2026-09-08 on the user's steer): everything about the **selected** sample.

Top: how it differs from the **anchor**, axis by axis — read-only bars in %
of the library's spread, a tooltip on every axis saying what it measures.
Then its CLAP chips (click one to search), its ten best CLAP tag scores as
bars, and its embedding as stripes. Everything else moved out on the
user's steer (2026-09-08): the search box, the filters and the weight bars
to the Search tab (`search.py`), the caption to the top of the waveform
panel, and the segments table went — the waveform shows them.

The panel owns no data: it emits what the user asked for and the window
applies it.
"""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QLabel,
    QLayout,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .embedding import CLASS_PROMPTS, TAG_PROMPT
from .similarity import AXES, AXIS_LABELS
from .theme import SqueezableWidget
from .vectorstrip import VectorStrip


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
TAG_CAPTION = (
    f"Cosine ×100 of the sample's CLAP embedding against each tag prompt “{TAG_PROMPT.format('…')}”, "
    "best first — the numbers behind the chips above, ten of the thirty-two."
)
CHIP_COUNT = 5        # chips shown (click to search)
TAG_BAR_COUNT = 10    # tag scores shown as bars


def _prompts_text(name: str) -> str:
    return "Best of these prompts:\n" + "\n".join(f"• {p}" for p in CLASS_PROMPTS[name])


class AttributesPanel(QWidget):
    search_requested = Signal(str)      # a chip was clicked: search for its tag

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

        # (2) the selected sample's chips (its caption is the waveform panel's line, 2026-09-08)
        search_group = QGroupBox("Tags of the selected sample")
        search_layout = QVBoxLayout(search_group)
        self._tags_label = QLabel("CLAP zero-shot chips — click one to search for it (Search tab):")
        self._tags_label.setWordWrap(True)
        self._tags_grid = QGridLayout()
        self._tags_grid.setHorizontalSpacing(4)
        self._tag_buttons: list[QPushButton] = []
        search_layout.addWidget(self._tags_label)
        search_layout.addLayout(self._tags_grid)

        # (2b) CLAP's tag scores for the selected sample — the numbers behind the
        # chips (2026-09-08, the user's steer: these instead of the four class numbers)
        clap_group = QGroupBox("CLAP tag scores of the selected sample")
        clap_layout = QGridLayout(clap_group)
        clap_caption = QLabel(TAG_CAPTION)
        clap_caption.setWordWrap(True)
        clap_layout.addWidget(clap_caption, 0, 0, 1, 2)
        self._tag_rows: list[tuple[QLabel, QProgressBar]] = []
        for row in range(1, TAG_BAR_COUNT + 1):
            label = QLabel("")
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(True)
            bar.setMinimumWidth(60)
            bar.setFormat("n/a")
            clap_layout.addWidget(label, row, 0)
            clap_layout.addWidget(bar, row, 1)
            self._tag_rows.append((label, bar))
        clap_layout.setColumnStretch(1, 1)
        self._show_tag_scores([])

        # (2c) the embedding itself: 512 numbers as colour stripes
        strip_group = QGroupBox("CLAP embedding — the 512 numbers")
        strip_layout = QVBoxLayout(strip_group)
        self._strip = VectorStrip()
        self._strip.setToolTip(
            "The stored CLAP vector of the selected sample or hit: one stripe per dimension, "
            "amber positive, blue negative, scaled to its largest value. The anchor's vector "
            "is drawn underneath for comparison. `crate-embed --export FILE.npz` writes them all."
        )
        strip_layout.addWidget(self._strip)

        controls = SqueezableWidget()
        controls_layout = QVBoxLayout(controls)
        # Let the contents squeeze to the pane instead of enforcing their
        # minimum: a five-checkbox row must never push the panel past its edge.
        controls_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        controls_layout.addWidget(diff_group)
        controls_layout.addWidget(search_group)
        controls_layout.addWidget(clap_group)
        controls_layout.addWidget(strip_group)
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

    def _show_tag_scores(self, tags: list[tuple[str, float]]) -> None:
        """The selected sample's best tag cosines, as bars (×100, best first)."""
        for n, (label, bar) in enumerate(self._tag_rows):
            if n < len(tags):
                tag, score = tags[n]
                label.setText(tag)
                label.setToolTip(f"“{TAG_PROMPT.format(tag)}”: cosine {score:.3f}")
                bar.setValue(int(round(min(max(score, 0.0), 1.0) * 100)))
                bar.setFormat("%v")
                label.show()
                bar.show()
            else:
                label.setText("")
                bar.setValue(0)
                bar.setFormat("n/a")
                label.setVisible(n == 0)
                bar.setVisible(n == 0)

    def show_vector(self, vector, anchor=None) -> None:
        self._strip.show_vectors(vector, anchor)

    def tag_values(self) -> dict[str, int]:
        """What the tag bars show (tests read this): tag → cosine ×100."""
        return {
            label.text(): bar.value()
            for label, bar in self._tag_rows if label.text() and bar.format() != "n/a"
        }

    def show_tags(self, tags: list[tuple[str, float]]) -> None:
        """All of a sample's tags, best first: the first few as chips, the
        first ten as bars."""
        self._show_tag_scores(tags[:TAG_BAR_COUNT])
        tags = tags[:CHIP_COUNT]
        for button in self._tag_buttons:
            self._tags_grid.removeWidget(button)
            button.hide()                  # gone now, not at the next event-loop turn
            button.setParent(None)
            button.deleteLater()
        self._tag_buttons = []
        for n, (tag, score) in enumerate(tags):
            button = QPushButton(f"{tag}  {score * 100:.0f}")
            button.setFlat(True)
            button.setToolTip(f"CLAP zero-shot chip, cosine {score:.2f} — click to search for “{tag}”")
            button.clicked.connect(lambda _checked=False, t=tag: self.search_for(t))
            self._tags_grid.addWidget(button, n // 3, n % 3)   # three per row: the row wraps
            self._tag_buttons.append(button)

    def search_for(self, text: str) -> None:
        """A chip: hand the tag to the Search tab (the window routes it)."""
        self.search_requested.emit(text)

    # --- plumbing ---
