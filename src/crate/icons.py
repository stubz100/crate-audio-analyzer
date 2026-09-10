"""Icons drawn with QPainter, tinted per state (spec §9.1, layer 2).

2026-09-10. The window's icons were text — ▶ ■ ▸ ▾ ⚓ ↳ — which the design
study flagged as item 8 of the diagnosis: a glyph cannot be tinted by state,
its weight and metrics shift with whatever face resolves it, and a face that
lacks it draws a box (⚓ and ↳ rasterised as boxes in the first gallery grab,
before `seguisym.ttf` joined `design.FONT_FILES`).

These are paths instead. `icon(name, colour, size)` returns a `QIcon` drawn
at the colour asked for, so a control tints its icon from the token that
matches its state, and `pixmap()` gives the same for a painter that wants to
blit one. Everything is drawn in a 0..1 unit square and scaled, so any size
is crisp; strokes are scaled with it and antialiased.

No SVG and no asset files: Qt's own painter is enough for shapes this simple,
and it keeps the package a pure import with nothing to package or find on
disk at runtime.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

DEFAULT_SIZE = 16


def _play(path: QPainterPath) -> None:
    path.moveTo(0.24, 0.14)
    path.lineTo(0.84, 0.50)
    path.lineTo(0.24, 0.86)
    path.closeSubpath()


def _stop(path: QPainterPath) -> None:
    path.addRoundedRect(QRectF(0.20, 0.20, 0.60, 0.60), 0.06, 0.06)


def _anchor(path: QPainterPath) -> None:
    """The list's ⚓ — a ring, filled by the caller when it is the anchor."""
    path.addEllipse(QRectF(0.18, 0.18, 0.64, 0.64))
    path.addEllipse(QRectF(0.34, 0.34, 0.32, 0.32))


def _dot(path: QPainterPath) -> None:
    path.addEllipse(QRectF(0.30, 0.30, 0.40, 0.40))


def _chevron_right(path: QPainterPath) -> None:
    path.moveTo(0.38, 0.22)
    path.lineTo(0.66, 0.50)
    path.lineTo(0.38, 0.78)


def _chevron_down(path: QPainterPath) -> None:
    path.moveTo(0.22, 0.38)
    path.lineTo(0.50, 0.66)
    path.lineTo(0.78, 0.38)


def _waveform(path: QPainterPath) -> None:
    for i, height in enumerate((0.22, 0.42, 0.30, 0.50, 0.34, 0.18)):
        x = 0.16 + i * 0.14
        path.moveTo(x, 0.50 - height)
        path.lineTo(x, 0.50 + height)


def _spectrum(path: QPainterPath) -> None:
    for i, height in enumerate((0.26, 0.46, 0.62, 0.38, 0.20)):
        x = 0.18 + i * 0.16
        path.addRect(QRectF(x - 0.045, 0.84 - height, 0.09, height))


def _save(path: QPainterPath) -> None:
    path.addRoundedRect(QRectF(0.16, 0.16, 0.68, 0.68), 0.08, 0.08)
    path.addRect(QRectF(0.34, 0.16, 0.32, 0.24))
    path.addRect(QRectF(0.30, 0.54, 0.40, 0.30))


def _trash(path: QPainterPath) -> None:
    path.addRect(QRectF(0.22, 0.26, 0.56, 0.08))
    path.addRoundedRect(QRectF(0.28, 0.34, 0.44, 0.50), 0.05, 0.05)
    path.addRect(QRectF(0.40, 0.18, 0.20, 0.08))


def _close(path: QPainterPath) -> None:
    path.moveTo(0.26, 0.26)
    path.lineTo(0.74, 0.74)
    path.moveTo(0.74, 0.26)
    path.lineTo(0.26, 0.74)


def _search(path: QPainterPath) -> None:
    path.addEllipse(QRectF(0.18, 0.18, 0.46, 0.46))
    path.moveTo(0.60, 0.60)
    path.lineTo(0.82, 0.82)


def _folder(path: QPainterPath) -> None:
    path.moveTo(0.14, 0.76)
    path.lineTo(0.14, 0.26)
    path.lineTo(0.42, 0.26)
    path.lineTo(0.50, 0.36)
    path.lineTo(0.86, 0.36)
    path.lineTo(0.86, 0.76)
    path.closeSubpath()


def _refresh(path: QPainterPath) -> None:
    path.arcMoveTo(QRectF(0.20, 0.20, 0.60, 0.60), 40.0)
    path.arcTo(QRectF(0.20, 0.20, 0.60, 0.60), 40.0, 280.0)


def _drag_out(path: QPainterPath) -> None:
    path.moveTo(0.30, 0.70)
    path.lineTo(0.74, 0.26)
    path.moveTo(0.48, 0.26)
    path.lineTo(0.74, 0.26)
    path.lineTo(0.74, 0.52)


#: name → (draw, filled). A filled icon is a shape; an unfilled one is a
#: stroke, so a chevron and a play triangle can share the same machinery.
SHAPES = {
    "play": (_play, True),
    "stop": (_stop, True),
    "anchor": (_anchor, False),
    "dot": (_dot, True),
    "chevron-right": (_chevron_right, False),
    "chevron-down": (_chevron_down, False),
    "waveform": (_waveform, False),
    "spectrum": (_spectrum, True),
    "save": (_save, False),
    "trash": (_trash, False),
    "close": (_close, False),
    "search": (_search, False),
    "folder": (_folder, False),
    "refresh": (_refresh, False),
    "drag-out": (_drag_out, False),
}


def pixmap(
    name: str,
    colour: QColor,
    size: int = DEFAULT_SIZE,
    *,
    stroke: float = 1.5,
) -> QPixmap:
    """One icon at one colour and size, on a transparent ground.

    `stroke` is in pixels at `DEFAULT_SIZE` and scales with the icon, so the
    same weight reads at 12px and at 32px.
    """
    if name not in SHAPES:
        raise KeyError(f"no icon named {name!r}")
    draw, filled = SHAPES[name]
    result = QPixmap(size, size)
    result.setDevicePixelRatio(1.0)
    result.fill(Qt.GlobalColor.transparent)

    path = QPainterPath()
    draw(path)

    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(QPointF(0.5, 0.5))            # crisp odd-width strokes
    painter.scale(size - 1, size - 1)
    if filled:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
    else:
        pen = QPen(colour)
        pen.setWidthF(stroke / DEFAULT_SIZE)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.end()
    return result


def icon(
    name: str,
    colour: QColor,
    size: int = DEFAULT_SIZE,
    *,
    disabled: QColor | None = None,
    stroke: float = 1.5,
) -> QIcon:
    """A `QIcon` carrying its normal and (optionally) disabled colours."""
    result = QIcon(pixmap(name, colour, size, stroke=stroke))
    if disabled is not None:
        result.addPixmap(
            pixmap(name, disabled, size, stroke=stroke),
            QIcon.Mode.Disabled,
            QIcon.State.Off,
        )
    return result
