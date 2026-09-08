"""The header's tag-score bars (2026-09-08, the user's steer): the selected
sample's ten best CLAP zero-shot tags as vertical bars — the score on top,
the tag under — across the window's header, clickable like the chips: a
click hands the tag to the Search tab. A bar's height is the cosine ×100
against a full-height 100, so two samples' bars compare directly.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from .embedding import TAG_PROMPT
from .theme import ACCENT, TEXT, TEXT_DIM, WHITE, mix

BAR_COUNT = 10
_VALUE_H = 13.0
_LABEL_H = 14.0


class TagBars(QWidget):
    tag_clicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tags: list[tuple[str, float]] = []
        self._hover = -1
        self.setMouseTracking(True)
        self.setMinimumHeight(int(_VALUE_H + _LABEL_H + 24))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip("The selected sample's best CLAP tags, cosine ×100 — click one to search for it.")

    # --- data ---

    def show_tags(self, tags: list[tuple[str, float]]) -> None:
        self._tags = list(tags[:BAR_COUNT])
        self._hover = -1
        self.update()

    @property
    def tags(self) -> list[tuple[str, float]]:
        return list(self._tags)

    # --- geometry ---

    def slot_at(self, x: float) -> int:
        """Which bar's slot a widget x falls in; -1 outside."""
        n = len(self._tags)
        if not n or self.width() <= 0:
            return -1
        i = int(x // (self.width() / n))
        return i if 0 <= i < n else -1

    # --- painting ---

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self._tags:
            painter.setPen(TEXT_DIM)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "select a sample to see its CLAP tag scores")
            painter.end()
            return
        n = len(self._tags)
        slot = self.width() / n
        bar_area = max(self.height() - _VALUE_H - _LABEL_H - 4, 4.0)
        metrics = painter.fontMetrics()
        for i, (tag, score) in enumerate(self._tags):
            x0 = i * slot
            value = min(max(score, 0.0), 1.0)
            height = bar_area * value
            top = _VALUE_H + 2 + (bar_area - height)
            colour = mix(ACCENT, WHITE, 0.35) if i == self._hover else ACCENT
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawRoundedRect(QRectF(x0 + slot * 0.25, top, slot * 0.5, height), 2.0, 2.0)
            painter.setPen(TEXT)
            painter.drawText(QRectF(x0, top - _VALUE_H - 1, slot, _VALUE_H), Qt.AlignmentFlag.AlignHCenter, f"{score * 100:.0f}")
            painter.setPen(TEXT if i == self._hover else TEXT_DIM)
            label = metrics.elidedText(tag, Qt.TextElideMode.ElideRight, int(slot) - 4)
            painter.drawText(QRectF(x0, self.height() - _LABEL_H - 1, slot, _LABEL_H), Qt.AlignmentFlag.AlignHCenter, label)
        painter.end()

    # --- interaction ---

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        i = self.slot_at(event.position().x())
        if i != self._hover:
            self._hover = i
            if i >= 0:
                tag, score = self._tags[i]
                self.setToolTip(f"“{TAG_PROMPT.format(tag)}”: cosine {score:.3f} — click to search for “{tag}”")
            self.setCursor(Qt.CursorShape.PointingHandCursor if i >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        i = self.slot_at(event.position().x())
        if i >= 0:
            self.tag_clicked.emit(self._tags[i][0])
