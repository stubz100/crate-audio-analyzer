"""The map view (spec §9.3, Phase 6): one point per **sample** — segments
never get a point (§6.4) — positioned by the last explicitly computed layout
(`layout.py`), coloured by content class, shaped by structural type.

Clicking a point selects that sample in the window (which previews it);
double-clicking plays it. If anchored and ranked, the nearest neighbours
carry a halo — the *last computed* ranking, never live (§9.6). A small
**segment-match badge** marks a sample whose current search or ranking hit
landed on one of its segments rather than the sample itself.

Plain QWidget painting rather than a QGraphicsScene: a few thousand points
paint in milliseconds, and pan/zoom is one affine transform.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPen, QWheelEvent
from PySide6.QtWidgets import QToolTip, QWidget

CLASS_COLOURS: dict[str, QColor] = {
    "rhythmic": QColor(230, 120, 40),
    "melodic": QColor(50, 110, 220),
    "vocal": QColor(200, 60, 160),
    "other": QColor(60, 160, 90),
    "": QColor(150, 150, 150),        # unclassified / flagged
}
CLASS_ORDER = ("rhythmic", "melodic", "vocal", "other", "")
TYPE_LABELS = {"one-shot": "circle", "multi-hit": "square", "loop": "diamond", "": "dot"}

_POINT = 5.0        # half-size of a marker, px
_HALO = 10.0
_PICK_RADIUS = 10.0
_MARGIN = 30.0


def _marker(path_type: str, x: float, y: float, r: float) -> QPainterPath:
    path = QPainterPath()
    if path_type == "multi-hit":
        path.addRect(QRectF(x - r, y - r, 2 * r, 2 * r))
    elif path_type == "loop":
        path.moveTo(x, y - r * 1.3)
        path.lineTo(x + r * 1.3, y)
        path.lineTo(x, y + r * 1.3)
        path.lineTo(x - r * 1.3, y)
        path.closeSubpath()
    elif path_type == "one-shot":
        path.addEllipse(QPointF(x, y), r, r)
    else:
        path.addEllipse(QPointF(x, y), r * 0.6, r * 0.6)
    return path


class MapView(QWidget):
    sample_clicked = Signal(int)
    sample_activated = Signal(int)     # double-click → play

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(200, 150)
        self._ids = np.zeros(0, dtype=np.int64)
        self._xy = np.zeros((0, 2), dtype=np.float64)
        self._names: list[str] = []
        self._classes: list[str] = []
        self._types: list[str] = []
        self._visible = np.zeros(0, dtype=bool)
        self._index_of: dict[int, int] = {}
        self._selected: int | None = None
        self._anchor: int | None = None
        self._halo: set[int] = set()
        self._badges: set[int] = set()
        self._caption = "no layout yet — Recompute tab → Recompute map layout"
        self._scale = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._press: QPointF | None = None
        self._dragged = False

    # --- data in ---

    def set_points(
        self,
        ids: list[int],
        xy: np.ndarray,
        names: list[str],
        classes: list[str],
        types: list[str],
    ) -> None:
        self._ids = np.asarray(ids, dtype=np.int64)
        self._xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
        self._names = list(names)
        self._classes = list(classes)
        self._types = list(types)
        self._visible = np.ones(len(self._ids), dtype=bool)
        self._index_of = {int(i): n for n, i in enumerate(self._ids)}
        self.fit()

    def set_caption(self, text: str) -> None:
        self._caption = text
        self.update()

    def set_visible(self, ids: set[int] | None) -> None:
        """The filtered set (§9: one shared filtered dataset); None = all."""
        if ids is None:
            self._visible = np.ones(len(self._ids), dtype=bool)
        else:
            self._visible = np.isin(self._ids, np.fromiter(ids, dtype=np.int64))
        self.update()

    def set_selected(self, sample_id: int | None) -> None:
        self._selected = sample_id
        self.update()

    def set_anchor(self, sample_id: int | None) -> None:
        self._anchor = sample_id
        self.update()

    def set_halo(self, ids: set[int]) -> None:
        self._halo = set(ids)
        self.update()

    def set_badges(self, ids: set[int]) -> None:
        self._badges = set(ids)
        self.update()

    @property
    def point_count(self) -> int:
        return len(self._ids)

    @property
    def visible_count(self) -> int:
        return int(self._visible.sum())

    # --- geometry ---

    def fit(self) -> None:
        """Scale and pan so every point fits the widget with a margin."""
        if len(self._ids) == 0:
            self._scale, self._pan = 1.0, QPointF(0.0, 0.0)
            self.update()
            return
        lo = self._xy.min(axis=0)
        hi = self._xy.max(axis=0)
        span = np.maximum(hi - lo, 1e-9)
        width = max(self.width() - 2 * _MARGIN, 1.0)
        height = max(self.height() - 2 * _MARGIN, 1.0)
        self._scale = float(min(width / span[0], height / span[1]))
        centre = (lo + hi) / 2
        self._pan = QPointF(
            self.width() / 2 - centre[0] * self._scale,
            self.height() / 2 + centre[1] * self._scale,
        )
        self.update()

    def _to_widget(self, xy: np.ndarray) -> np.ndarray:
        out = np.empty_like(xy)
        out[:, 0] = xy[:, 0] * self._scale + self._pan.x()
        out[:, 1] = -xy[:, 1] * self._scale + self._pan.y()
        return out

    def _pick(self, pos: QPointF) -> int | None:
        if len(self._ids) == 0:
            return None
        pts = self._to_widget(self._xy)
        d2 = (pts[:, 0] - pos.x()) ** 2 + (pts[:, 1] - pos.y()) ** 2
        d2[~self._visible] = np.inf
        i = int(np.argmin(d2))
        if d2[i] > _PICK_RADIUS ** 2:
            return None
        return int(self._ids[i])

    # --- painting ---

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(250, 250, 250))
        if len(self._ids) == 0:
            painter.setPen(QColor(90, 90, 90))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._caption)
            painter.end()
            return
        pts = self._to_widget(self._xy)
        halo_pen = QPen(QColor(240, 190, 40), 2.5)
        badge = QColor(30, 30, 30)
        for i in np.flatnonzero(self._visible):
            sid = int(self._ids[i])
            x, y = float(pts[i, 0]), float(pts[i, 1])
            if sid in self._halo:
                painter.setPen(halo_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(QPointF(x, y), _HALO, _HALO)
            colour = CLASS_COLOURS.get(self._classes[i] or "", CLASS_COLOURS[""])
            painter.setPen(QPen(colour.darker(130), 1.0))
            painter.setBrush(colour)
            painter.drawPath(_marker(self._types[i] or "", x, y, _POINT))
            if sid in self._badges:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(badge)
                painter.drawEllipse(QPointF(x + _POINT, y - _POINT), 2.5, 2.5)
        for sid, colour, width in ((self._anchor, QColor(20, 20, 20), 2.5), (self._selected, QColor(0, 90, 200), 2.0)):
            if sid is None or sid not in self._index_of:
                continue
            i = self._index_of[sid]
            if not self._visible[i]:
                continue
            painter.setPen(QPen(colour, width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            r = _POINT + (4 if sid == self._anchor else 2.5)
            painter.drawEllipse(QPointF(float(pts[i, 0]), float(pts[i, 1])), r, r)
        self._paint_legend(painter)
        painter.setPen(QColor(70, 70, 70))
        painter.drawText(QRectF(8, 6, self.width() - 16, 18), Qt.AlignmentFlag.AlignLeft, self._caption)
        painter.end()

    def _paint_legend(self, painter: QPainter) -> None:
        x, y = 12.0, self.height() - 14.0
        for name in CLASS_ORDER:
            colour = CLASS_COLOURS[name]
            painter.setPen(QPen(colour.darker(130), 1.0))
            painter.setBrush(colour)
            painter.drawEllipse(QPointF(x, y), 4.5, 4.5)
            painter.setPen(QColor(70, 70, 70))
            label = name or "unclassified"
            painter.drawText(QPointF(x + 9, y + 4), label)
            x += 24 + 6.5 * len(label)
        painter.drawText(QPointF(x + 6, y + 4), "● one-shot  ■ multi-hit  ◆ loop  · halo = ranked neighbour  • badge = hit inside")

    # --- interaction ---

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.fit()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position()
            self._dragged = False
        elif event.button() == Qt.MouseButton.RightButton:
            self.fit()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.position() - self._press
            if not self._dragged and (abs(delta.x()) + abs(delta.y())) < 3:
                return
            self._dragged = True
            self._pan += delta
            self._press = event.position()
            self.update()
            return
        sid = self._pick(event.position())
        if sid is not None:
            QToolTip.showText(event.globalPosition().toPoint(), self._names[self._index_of[sid]], self)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if not self._dragged:
            sid = self._pick(event.position())
            if sid is not None:
                self._selected = sid
                self.update()
                self.sample_clicked.emit(sid)
        self._press = None
        self._dragged = False

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        sid = self._pick(event.position())
        if sid is not None:
            self.sample_activated.emit(sid)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            return
        factor = 1.15 ** steps
        pos = event.position()
        # zoom about the cursor: keep the data point under it fixed
        self._pan = QPointF(
            pos.x() - (pos.x() - self._pan.x()) * factor,
            pos.y() - (pos.y() - self._pan.y()) * factor,
        )
        self._scale *= factor
        self.update()
