"""Icons drawn with QPainter, tinted per state (spec §9.1, layer 2).

2026-09-10. The window's icons were text — ▶ ■ ▸ ▾ ⚓ ↳ — which the design
study flagged as item 8 of the diagnosis: a glyph cannot be tinted by state,
its weight and metrics shift with whatever face resolves it, and a face that
lacks it draws a box (⚓ and ↳ rasterised as boxes in the first gallery grab,
before `seguisym.ttf` joined `design.FONT_FILES`).

These are paths instead. `icon(name, colour)` returns a `QIcon` whose engine
(`PathIconEngine`) redraws the path at whatever size and display scale the
style asks for, in the colour for the control's state — `disabled` when it
is, `on` when it is checked — so a control tints its icon from the token that
matches its state. `pixmap()` rasterises one for a painter that wants to blit
it. Everything is drawn in a 0..1 unit square and scaled, so any size is
crisp — on a display at 150 % or 200 % too, which a `QIcon` built from one
16 px raster was not (the 2026-09-12 review: Qt stretched it); strokes are
scaled with it and antialiased.

No SVG and no asset files: Qt's own painter is enough for shapes this simple,
and it keeps the package a pure import with nothing to package or find on
disk at runtime.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPainterPath, QPen, QPixmap

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


def _draw(painter: QPainter, name: str, colour: QColor, rect: QRectF, stroke: float) -> None:
    """Draw one icon into `rect`, centred, with the painter as it is.

    The painter's own transform is kept — including the device pixel ratio a
    high-DPI pixmap or a scaled window gives it — which is what makes the icon
    a vector rather than a raster.
    """
    if name not in SHAPES:
        raise KeyError(f"no icon named {name!r}")
    draw, filled = SHAPES[name]
    path = QPainterPath()
    draw(path)
    side = min(rect.width(), rect.height())

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(                              # the half pixel keeps odd-width strokes crisp
        rect.x() + (rect.width() - side) / 2 + 0.5,
        rect.y() + (rect.height() - side) / 2 + 0.5,
    )
    painter.scale(side - 1, side - 1)
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
    painter.restore()


def pixmap(
    name: str,
    colour: QColor,
    size: int = DEFAULT_SIZE,
    *,
    stroke: float = 1.5,
    scale: float = 1.0,
) -> QPixmap:
    """One icon at one colour and size, on a transparent ground.

    `stroke` is in pixels at `DEFAULT_SIZE` and scales with the icon, so the
    same weight reads at 12px and at 32px. `scale` is a device pixel ratio:
    the pixmap is `size × scale` pixels on a side and carries that ratio, so
    it draws at `size` logical pixels — crisp — on a display scaled by it.
    """
    if name not in SHAPES:
        raise KeyError(f"no icon named {name!r}")
    side = int(round(size * scale))
    result = QPixmap(side, side)
    result.setDevicePixelRatio(scale)              # a painter on it scales by this on its own
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    _draw(painter, name, colour, QRectF(0, 0, size, size), stroke)
    painter.end()
    return result


class PathIconEngine(QIconEngine):
    """A `QIcon` engine that draws the path on demand — a vector at every scale.

    A `QIcon` built from a pixmap holds one raster: asked for 16 px on a
    display at 200 % it hands back the same 16 × 16 and Qt stretches it to 32
    physical pixels, soft (the 2026-09-12 review). This engine redraws the
    path at whatever size and device pixel ratio the style asks for instead,
    which is what "any size is crisp" always meant.

    It also carries the icon's colours by state: `disabled` for a control
    that is, and `on` for a checked one — `QStyle` asks for `QIcon.State.On`
    when a button is checked, so a checked Spectrum no longer keeps a
    resting-grey icon under bright text.
    """

    def __init__(
        self,
        name: str,
        colour: QColor,
        *,
        disabled: QColor | None = None,
        on: QColor | None = None,
        stroke: float = 1.5,
    ) -> None:
        super().__init__()
        if name not in SHAPES:
            raise KeyError(f"no icon named {name!r}")
        self._name = name
        self._colour = QColor(colour)
        self._disabled = None if disabled is None else QColor(disabled)
        self._on = None if on is None else QColor(on)
        self._stroke = stroke

    def clone(self) -> QIconEngine:  # noqa: N802
        return PathIconEngine(
            self._name, self._colour, disabled=self._disabled, on=self._on, stroke=self._stroke
        )

    def colour_for(self, mode: QIcon.Mode, state: QIcon.State) -> QColor:
        if mode == QIcon.Mode.Disabled and self._disabled is not None:
            return self._disabled
        if state == QIcon.State.On and self._on is not None:
            return self._on
        return self._colour

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        _draw(painter, self._name, self.colour_for(mode, state), QRectF(rect), self._stroke)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(  # noqa: N802
        self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float
    ) -> QPixmap:
        """What `QIcon.pixmap(size, devicePixelRatio)` asks for: `size` is logical."""
        return pixmap(
            self._name,
            self.colour_for(mode, state),
            min(size.width(), size.height()),
            stroke=self._stroke,
            scale=scale,
        )


def icon(
    name: str,
    colour: QColor,
    *,
    disabled: QColor | None = None,
    on: QColor | None = None,
    stroke: float = 1.5,
) -> QIcon:
    """A `QIcon` drawn at any size and display scale: `colour` at rest,
    `disabled` when the control is, `on` when it is checked."""
    return QIcon(PathIconEngine(name, colour, disabled=disabled, on=on, stroke=stroke))
