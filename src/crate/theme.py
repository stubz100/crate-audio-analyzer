"""The application's look — applied once, and the widgets the layout needs.

2026-09-07, on the user's steer: one dark theme in the idiom of production
tools — Fusion style, a palette, a style sheet — plus the colours the painted
views (map, waveform) share with it, so the whole window reads as one surface
rather than default widgets.

2026-09-10, on the user's steer: the values themselves moved to `design.py`,
the design system's token layer (spec §9.1, `.docs/design_studies.md`). This
module is now the *application* of those tokens — `apply_theme()` — plus the
flat constants the painted views import.

Those constants are kept because the painters read them directly and want a
`QColor`, not a path through three dataclasses; but every one is now a view
onto a token, so there is exactly one place a colour is decided. Note two
renamings that the token layer made explicit and that the old flat namespace
could not express:

* **`BG` is the painters' canvas**, the darkest surface, not the window
  ground the widgets sit on (`design.TOKENS.surface.ground`). The old `BG`
  was asked to be both.
* **`AMBER`, `GREEN` and `PINK` are data colours** — what a segment, a manual
  segment and the envelope *are* — not chrome. `ACCENT` is now only ever
  state: selection, focus, the playhead.

`apply_theme(app)` is called once by `main()`; tests run unthemed. The
painters import the constants directly, so they look right either way.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from .design import TOKENS, font, load_fonts, mix, palette, stylesheet

# --- the palette, as the painted views want it -----------------------------

BG = TOKENS.surface.canvas          # what the map and waveform draw on
PANEL = TOKENS.surface.panel
FIELD = TOKENS.surface.sunken
RAISED = TOKENS.surface.raised
BORDER = TOKENS.surface.divider
TEXT = TOKENS.ink.primary
TEXT_DIM = TOKENS.ink.secondary
WHITE = TOKENS.ink.bright
ACCENT = TOKENS.state.accent        # selection, focus, the playhead
ACCENT_DIM = TOKENS.state.accent_dim
RED = TOKENS.state.danger
AMBER = TOKENS.data.segment         # segments, hits, halos
GREEN = TOKENS.data.segment_manual  # manual segments
PINK = TOKENS.data.envelope
UNSCORED = TOKENS.data.unscored
SCORE_LOW = TOKENS.data.score_low   # score gradient for the map: dim → bright
SCORE_HIGH = TOKENS.data.score_high

__all__ = [
    "ACCENT", "ACCENT_DIM", "AMBER", "BG", "BORDER", "ElidedLabel", "FIELD",
    "GREEN", "PANEL", "PINK", "RAISED", "RED", "SCORE_HIGH", "SCORE_LOW",
    "SqueezableWidget", "TEXT", "TEXT_DIM", "UNSCORED", "WHITE", "apply_theme",
    "mix",
]


class SqueezableWidget(QWidget):
    """A scroll area's content that never claims a minimum width: a
    QScrollArea sizes its widget to the viewport but not below the
    widget's `minimumSizeHint`, so a wide row (five checkboxes) would
    otherwise push the panel past its pane (2026-09-07)."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())


class ElidedLabel(QLabel):
    """A one-line label that never claims its text's width. A long file name
    in the transport row raised the left pane's minimum width and squeezed
    the right panel to its floor — and the splitter then remembered the
    squeeze (2026-09-08, the user's report). The text is elided in the middle
    to the space there is; the full text is the tooltip; `text()` is the full
    text, as callers expect."""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setToolTip(text)

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self.setToolTip(text)
        self.update()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        rect = self.contentsRect()
        elided = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideMiddle, rect.width())
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(rect, int(self.alignment()) | Qt.TextFlag.TextSingleLine, elided)
        painter.end()


def apply_theme(app: QApplication) -> None:
    """Fusion + the token palette + the token style sheet, once per application.

    `load_fonts()` registers the faces by path: a no-op on a real desktop
    where they are installed, and the difference between a legible and a
    tofu-box screenshot under the offscreen platform (`design.FONT_FILES`).
    """
    load_fonts()
    app.setStyle("Fusion")
    app.setPalette(palette())
    app.setFont(font("body"))
    app.setStyleSheet(stylesheet())
