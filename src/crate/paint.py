"""The design system's painting kit (spec §9.1, layer 3).

2026-09-12. The tokens gave the widgets one material and the components gave
the control rows one shape, but the *painted* surfaces — the map, the
waveform, the spectrogram, the tag bars, the vector strip, the list's
delegate — each still invented their own treatment of the same few problems:
a division, a selected row's mark, a value blended along a ramp, an ambient
texture that should recede. This is the shared answer to those four, so the
painters read as the same material as the widgets around them.

Deliberately small, and every function here has a caller. A painting kit that
grows speculative helpers is how a design system starts lying about itself.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QLineF, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen

from .design import TOKENS, mix

#: How far a signal is held off the top and bottom of its plot. The waveform
#: used to come within 2px of both edges, which read as clipped even when the
#: peak was well under full scale (the third adjustment to direction A,
#: `.docs/design_studies.md`).
PLOT_INSET = 8.0

#: The ceiling `damp()` blends to. Ambient texture — the CLAP strip's 512
#: stripes — was the highest-contrast thing in the window while carrying the
#: least actionable information (item 7 of the diagnosis).
TEXTURE_CEILING = 0.45


def hairline(painter: QPainter, a: QPointF, b: QPointF, colour: QColor | None = None) -> None:
    """A division that should barely register. One pen width, one colour."""
    painter.setPen(QPen(colour or TOKENS.surface.hairline, 1))
    painter.drawLine(QLineF(a, b))


def selection_edge(
    painter: QPainter,
    rect: QRectF,
    colour: QColor | None = None,
    width: float = 2.0,
) -> None:
    """The accent bar down the left of a selected row.

    Direction A quietened the selection from a saturated full-width bar to a
    low fill, which read *too* quietly on its own; this is the mark that puts
    the row back at a glance without the fill coming back.
    """
    painter.fillRect(
        QRectF(rect.left(), rect.top(), width, rect.height()),
        colour or TOKENS.state.accent,
    )


@lru_cache(maxsize=8)
def _ramp_cached(low: str, high: str, steps: int) -> tuple[QColor, ...]:
    a, b = QColor(low), QColor(high)
    return tuple(mix(a, b, i / max(steps - 1, 1)) for i in range(steps))


def ramp(low: QColor, high: QColor, steps: int = 256) -> tuple[QColor, ...]:
    """A cached lookup table between two token colours.

    The map blended a score with `mix()` per point, per repaint — thousands of
    interpolations a frame for at most `steps` distinct results.
    """
    return _ramp_cached(low.name(), high.name(), steps)


def along(ramp_: tuple[QColor, ...], t: float) -> QColor:
    """Index a ramp by a 0..1 value, clamped."""
    return ramp_[int(min(max(t, 0.0), 1.0) * (len(ramp_) - 1))]


def damp(t: float, ceiling: float = TEXTURE_CEILING) -> float:
    """Compress a 0..1 intensity so a texture recedes instead of shouting.

    Used where the *pattern* is the information and the individual value is
    not — the CLAP strip reads as a fingerprint either way, and at full
    contrast it simply took the eye away from the waveform and the list.
    """
    return min(max(t, 0.0), 1.0) * ceiling
