"""The embedding strip (2026-09-07, the user's steer): the selected item's
CLAP vector — 512 numbers, the sample's "DNA" — drawn as 512 colour stripes,
with the anchor's vector underneath when there is one, so two sounds can be
compared by eye and not only by a cosine.

Colour: negative values towards the accent blue, positive towards amber,
zero the panel ground; the scale is the vector's own largest magnitude, so
every strip uses its full contrast (unit vectors peak around ±0.2).
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QWidget

from .design import TOKENS
from .paint import damp
from .theme import ACCENT, AMBER, TEXT, TEXT_DIM, mix

SUNKEN = TOKENS.surface.sunken

_ROW = 18.0
_GAP = 4.0
_HEADER = 16.0


class VectorStrip(QWidget):
    """`compact` (the window's header, 2026-09-08): no caption, thinner rows."""

    def __init__(self, parent=None, compact: bool = False) -> None:
        super().__init__(parent)
        self._row = 10.0 if compact else _ROW
        self._header = 0.0 if compact else _HEADER
        self._vector: np.ndarray | None = None
        self._anchor: np.ndarray | None = None
        self._caption = "select a sample or a hit to see its CLAP embedding"
        self.setMinimumHeight(int(self._header + self._row + 4))

    def show_vectors(
        self,
        vector: np.ndarray | None,
        anchor: np.ndarray | None = None,
        caption: str = "",
    ) -> None:
        self._vector = None if vector is None else np.asarray(vector, dtype=np.float32).reshape(-1)
        self._anchor = None if anchor is None else np.asarray(anchor, dtype=np.float32).reshape(-1)
        rows = 1 + (1 if self._anchor is not None else 0)
        self.setMinimumHeight(int(self._header + rows * self._row + (rows - 1) * _GAP + 4))
        if self._vector is None:
            self._caption = caption or "no CLAP embedding for this item yet"
        else:
            text = caption or f"{self._vector.size} numbers"
            if self._anchor is not None and self._anchor.size == self._vector.size:
                cosine = float(np.dot(self._vector, self._anchor))
                text += f" · cosine to anchor {cosine:.3f} (its stripes below)"
            self._caption = text
        self.update()

    @property
    def dimensions(self) -> int:
        return 0 if self._vector is None else int(self._vector.size)

    def _paint_row(self, painter: QPainter, vector: np.ndarray, top: float) -> None:
        width = float(self.width() - 8)
        scale = float(np.abs(vector).max()) or 1.0
        n = vector.size
        step = width / max(n, 1)
        painter.setPen(Qt.PenStyle.NoPen)
        for i, value in enumerate(vector):
            # Damped (2026-09-12, layer 3): at full contrast these 512 stripes
            # were the loudest thing in the window while carrying the least
            # actionable information — item 7 of the diagnosis. The pattern is
            # what is read here, not any single number, and it survives being
            # quiet. The blend also starts from the sunken ground rather than
            # the field, so the strip sits *in* the header instead of on it.
            t = damp(min(abs(float(value)) / scale, 1.0))
            colour = mix(SUNKEN, AMBER if value >= 0 else ACCENT, t)
            painter.setBrush(colour)
            painter.drawRect(QRectF(4 + i * step, top, max(step, 1.0), self._row))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(TOKENS.surface.hairline, 1))
        painter.drawRect(QRectF(4, top, width, self._row))

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        if self._header:
            painter.setPen(TEXT if self._vector is not None else TEXT_DIM)
            painter.drawText(QRectF(4, 0, self.width() - 8, self._header), Qt.AlignmentFlag.AlignLeft, self._caption)
        if self._vector is None:
            painter.end()
            return
        top = self._header
        self._paint_row(painter, self._vector, top)
        if self._anchor is not None:
            top += self._row + _GAP
            self._paint_row(painter, self._anchor, top)
        painter.end()
