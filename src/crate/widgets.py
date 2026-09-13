"""The design system's components (spec §9.1, layer 2).

2026-09-10. The direction studies showed the ceiling of a token layer: the
transport row was equally ragged under all four directions, the Attributes
panel was five label-plus-empty-bar rows in every one, and the tag bars never
changed. Colour, density, type and material are the tokens' to fix; *those*
are components' — items 5 to 8 of the diagnosis in `.docs/design_studies.md`.

What each one is for, in the order the diagnosis raised them:

* `Toolbar` — a control row where widths are the row's decision rather than
  each label's, groups are separated by a rule, and a destructive action
  looks destructive (`intent="danger"`). The transport row's six ungrouped,
  self-sized buttons were item 5.
* `SegmentedControl` — one control for an exclusive choice, painted rather
  than assembled, replacing the two stacked buttons of the view switch.
* `Meter` / `MeterList` — one implementation of "a labelled bar with a
  value", for the tag scores and the per-axis differences, which are the
  same object drawn two different ways today.
* `SectionHeader` — the uppercase micro-label with a hairline, because
  `text-transform` never reached `QGroupBox::title` (see `design.py`).
* `Chip`, `StatusPill`, `HelpText` — a searchable tag, a state readout, and
  prose that folds away instead of sitting in primary UI.

Everything reads its colours, spacing and type from `design.TOKENS`; nothing
here holds a value of its own.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .design import TOKENS, font, mix
from .icons import icon

# --- headings and prose ----------------------------------------------------


class SectionHeader(QWidget):
    """An uppercase micro-label with a hairline running out to the right.

    The label carries the transform itself: `text-transform` reaches a
    `QLabel` but not the `QGroupBox::title` subcontrol (checked by rendering,
    2026-09-10), so a group box could never have had this look.
    """

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self._label = QLabel(text)
        self._label.setObjectName("sectionHeader")
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setStyleSheet(f"color: {TOKENS.surface.hairline.name()};")
        rule.setFixedHeight(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, TOKENS.metric.sm, 0, TOKENS.metric.xs)
        layout.setSpacing(TOKENS.metric.md)
        layout.addWidget(self._label)
        layout.addWidget(rule, stretch=1)

    def setText(self, text: str) -> None:  # noqa: N802
        self._label.setText(text)

    def text(self) -> str:
        return self._label.text()


class HelpText(QWidget):
    """Prose that folds away — a "?" toggles it.

    The Recompute tab explains its folder ticks in a paragraph of body text
    sitting above the list (item 6 of the diagnosis). Help belongs a click
    away, not in the primary surface.
    """

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self._button = QPushButton("?")
        self._button.setCheckable(True)
        self._button.setFlat(True)
        self._button.setFixedWidth(22)
        self._button.setToolTip("What this does")
        self._body = QLabel(text)
        self._body.setWordWrap(True)
        self._body.setObjectName("caption")
        self._body.hide()
        self._button.toggled.connect(self._body.setVisible)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(TOKENS.metric.sm)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self._button)
        top.addStretch(1)
        layout.addLayout(top)
        layout.addWidget(self._body)

    def is_open(self) -> bool:
        return self._button.isChecked()

    def set_open(self, open_: bool) -> None:
        self._button.setChecked(open_)


# --- small readouts --------------------------------------------------------


class Chip(QPushButton):
    """A searchable tag. Flat, pill-shaped, quiet until hovered."""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(font("label"))


class FlowLayout(QLayout):
    """Items in rows that wrap at the width — for chips, which are many and
    short, and must never widen the pane (Phase 11, 2026-09-13)."""

    def __init__(self, parent=None, spacing: int | None = None) -> None:
        super().__init__(parent)
        self._items: list = []
        self._gap = TOKENS.metric.sm if spacing is None else spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def clear(self) -> None:
        """Take every widget out and let it go."""
        while self._items:
            item = self._items.pop()
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), place=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, place=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _arrange(self, rect: QRect, place: bool) -> int:
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        right = rect.right() - m.right()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > right and line_height > 0:
                x = rect.x() + m.left()
                y += line_height + self._gap
                line_height = 0
            if place:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._gap
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


class TagChip(QWidget):
    """A tag as a pill (Phase 11, 2026-09-13).

    Curated (`removable`): the text is a search, the × at its end takes the
    tag off the sample. Suggested (`suggested`): the machine's chip, quieter,
    with a + — a click promotes it into the curated layer (§8's flow).
    """

    clicked = Signal(str)
    removed = Signal(str)

    HEIGHT = 22
    PAD = 9
    GLYPH = 12

    def __init__(self, text: str, *, removable: bool = False, suggested: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._text = text
        self._removable = removable
        self._suggested = suggested
        self._hover = False
        self._hover_glyph = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if suggested:
            self.setToolTip(f"Add “{text}” to my tags")
        elif removable:
            self.setToolTip(f"Search for “{text}” — the × takes it off this sample")

    def text(self) -> str:
        return self._text

    @property
    def has_glyph(self) -> bool:
        return self._removable or self._suggested

    def sizeHint(self) -> QSize:  # noqa: N802
        width = QFontMetrics(font("label")).horizontalAdvance(self._text) + 2 * self.PAD
        if self.has_glyph:
            width += self.GLYPH + 2
        return QSize(width, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def _glyph_rect(self) -> QRectF:
        return QRectF(self.width() - self.PAD - self.GLYPH, (self.height() - self.GLYPH) / 2, self.GLYPH, self.GLYPH)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        s, ink, st = TOKENS.surface, TOKENS.ink, TOKENS.state
        radius = self.height() / 2
        if self._suggested:
            painter.setPen(QPen(s.divider if self._hover else s.hairline, 1))
            painter.setBrush(s.raised if self._hover else Qt.BrushStyle.NoBrush)
            text_colour = ink.primary if self._hover else ink.muted
        else:
            painter.setPen(QPen(st.accent_dim if self._hover else s.divider, 1))
            painter.setBrush(s.overlay if self._hover else s.raised)
            text_colour = ink.bright if self._hover else ink.primary
        painter.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), radius, radius)
        painter.setFont(font("label"))
        painter.setPen(text_colour)
        text_rect = QRectF(self.PAD, 0, self.width() - 2 * self.PAD - (self.GLYPH + 2 if self.has_glyph else 0), self.height())
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self._text)
        if self.has_glyph:
            g = self._glyph_rect().adjusted(3, 3, -3, -3)
            colour = st.danger if (self._removable and self._hover_glyph) else (ink.primary if self._hover else ink.muted)
            painter.setPen(QPen(colour, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            if self._removable:
                painter.drawLine(g.topLeft(), g.bottomRight())
                painter.drawLine(g.topRight(), g.bottomLeft())
            else:
                centre = g.center()
                painter.drawLine(QPointF(g.left(), centre.y()), QPointF(g.right(), centre.y()))
                painter.drawLine(QPointF(centre.x(), g.top()), QPointF(centre.x(), g.bottom()))
        painter.end()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        over_glyph = self._removable and self._glyph_rect().contains(event.position())
        if over_glyph != self._hover_glyph:
            self._hover_glyph = over_glyph
            self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = self._hover_glyph = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._removable and self._glyph_rect().contains(event.position()):
            self.removed.emit(self._text)
        else:
            self.clicked.emit(self._text)


class StatusPill(QLabel):
    """A one-word state readout — `neutral`, `active`, `warn`, `danger`."""

    TONES = {
        "neutral": (TOKENS.ink.muted, TOKENS.surface.raised),
        "active": (TOKENS.state.accent, TOKENS.state.accent_fill),
        "warn": (TOKENS.data.segment, TOKENS.surface.raised),
        "danger": (TOKENS.state.danger, TOKENS.surface.raised),
    }

    def __init__(self, text: str, tone: str = "neutral", parent=None) -> None:
        super().__init__(text, parent)
        self.setFont(font("micro"))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        if tone not in self.TONES:
            raise KeyError(f"no tone named {tone!r}")
        self._tone = tone
        ink, ground = self.TONES[tone]
        m = TOKENS.metric
        self.setStyleSheet(
            f"color: {ink.name()}; background: {ground.name()};"
            f"border-radius: {m.radius_md}px; padding: {m.xs}px {m.md - 2}px;"
            f"text-transform: uppercase; letter-spacing: {TOKENS.type.tracking};"
        )

    def tone(self) -> str:
        return self._tone


# --- meters ----------------------------------------------------------------


class Meter(QWidget):
    """A labelled bar with its value — the tag scores and the per-axis
    differences are the same object, drawn two different ways today.

    Value is 0..1. A meter with no value shows its track and an em dash,
    rather than the wide empty box with a centred "n/a" the Attributes tab
    uses now.
    """

    LABEL_W = 76
    VALUE_W = 40

    clicked = Signal(str)

    def __init__(self, label: str, value: float | None = None, tone: QColor | None = None, parent=None) -> None:
        super().__init__(parent)
        self._label = label
        self._value = value
        self._tone = tone or TOKENS.state.accent
        self._hover = False
        self._clickable = False
        self.setMinimumHeight(18)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # --- data ---

    def set_value(self, value: float | None) -> None:
        self._value = value
        self.update()

    def value(self) -> float | None:
        return self._value

    def label(self) -> str:
        return self._label

    def set_clickable(self, clickable: bool) -> None:
        self._clickable = clickable
        self.setMouseTracking(clickable)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor
        )

    # --- painting ---

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        m, ink = TOKENS.metric, TOKENS.ink
        height = self.height()
        track_x = self.LABEL_W + m.md
        track_w = max(self.width() - track_x - self.VALUE_W - m.md, 10)
        track_h = 6.0
        track_y = (height - track_h) / 2

        painter.setFont(font("label"))
        painter.setPen(ink.primary if self._hover else ink.secondary)
        metrics = QFontMetrics(painter.font())
        painter.drawText(
            QRectF(0, 0, self.LABEL_W, height),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(self._label, Qt.TextElideMode.ElideRight, self.LABEL_W),
        )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(TOKENS.surface.sunken)
        painter.drawRoundedRect(QRectF(track_x, track_y, track_w, track_h), 3.0, 3.0)

        if self._value is None:
            painter.setPen(TOKENS.ink.muted)
            painter.drawText(
                QRectF(self.width() - self.VALUE_W, 0, self.VALUE_W, height),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                "—",
            )
            painter.end()
            return

        fraction = min(max(self._value, 0.0), 1.0)
        colour = mix(self._tone, TOKENS.ink.bright, 0.3) if self._hover else self._tone
        painter.setBrush(colour)
        painter.drawRoundedRect(
            QRectF(track_x, track_y, max(track_w * fraction, 2.0), track_h), 3.0, 3.0
        )
        painter.setFont(font("body"))
        painter.setPen(ink.primary)
        painter.drawText(
            QRectF(self.width() - self.VALUE_W, 0, self.VALUE_W, height),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            f"{self._value * 100:.0f}",
        )
        painter.end()

    # --- interaction ---

    def enterEvent(self, event) -> None:  # noqa: N802
        if self._clickable:
            self._hover = True
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._label)


class MeterList(QWidget):
    """A column of `Meter`s that keeps its rows in step."""

    clicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(TOKENS.metric.xs)
        self._meters: list[Meter] = []

    def set_rows(
        self,
        rows: list[tuple[str, float | None]],
        *,
        tone: QColor | None = None,
        clickable: bool = False,
    ) -> None:
        while self._meters:
            meter = self._meters.pop()
            self._layout.removeWidget(meter)
            meter.deleteLater()
        for label, value in rows:
            meter = Meter(label, value, tone)
            meter.set_clickable(clickable)
            meter.clicked.connect(self.clicked)
            self._layout.addWidget(meter)
            self._meters.append(meter)

    @property
    def meters(self) -> list[Meter]:
        return list(self._meters)


# --- control rows ----------------------------------------------------------


class SegmentedControl(QWidget):
    """One control for an exclusive choice, painted as a single unit.

    The view switch was two stacked buttons that happened to be mutually
    exclusive; this is the choice itself. Options are `(key, label)` pairs.
    """

    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], parent=None) -> None:
        super().__init__(parent)
        if not options:
            raise ValueError("a segmented control needs at least one option")
        self._options = list(options)
        self._current: str | None = self._options[0][0]
        self._hover = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    # --- data ---

    def current(self) -> str | None:
        return self._current

    def set_current(self, key: str | None) -> None:
        """`None` shows no choice at all — a facet the machine has not
        decided yet (Phase 11). Only a real key is announced."""
        if key is not None and key not in dict(self._options):
            raise KeyError(f"no option named {key!r}")
        if key != self._current:
            self._current = key
            self.update()
            if key is not None:
                self.changed.emit(key)

    def sizeHint(self) -> QSize:  # noqa: N802
        metrics = QFontMetrics(font("label"))
        widest = max(metrics.horizontalAdvance(label) for _, label in self._options)
        pad = TOKENS.metric.lg * 2
        return QSize((widest + pad) * len(self._options), metrics.height() + TOKENS.metric.md)

    def _index_at(self, x: float) -> int:
        if self.width() <= 0:
            return -1
        i = int(x // (self.width() / len(self._options)))
        return i if 0 <= i < len(self._options) else -1

    # --- painting ---

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        m, s = TOKENS.metric, TOKENS.surface
        slot = self.width() / len(self._options)
        painter.setPen(QPen(s.divider, 1))
        painter.setBrush(s.raised)
        painter.drawRoundedRect(
            QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), m.radius_md, m.radius_md
        )
        painter.setFont(font("label"))
        for i, (key, label) in enumerate(self._options):
            rect = QRectF(i * slot, 0, slot, self.height())
            if key == self._current:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(TOKENS.state.accent_fill)
                painter.drawRoundedRect(
                    rect.adjusted(1.5, 1.5, -1.5, -1.5), m.radius_sm, m.radius_sm
                )
                painter.setPen(TOKENS.state.accent)
            elif i == self._hover:
                painter.setPen(TOKENS.ink.primary)
            else:
                painter.setPen(TOKENS.ink.muted)
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), label)
            if i and self._current not in (key, self._options[i - 1][0]):
                painter.setPen(QPen(s.hairline, 1))
                painter.drawLine(
                    QRectF(i * slot, m.sm, 0, self.height() - m.md).topLeft(),
                    QRectF(i * slot, m.sm, 0, self.height() - m.md).bottomLeft(),
                )
        painter.end()

    # --- interaction ---

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        i = self._index_at(event.position().x())
        if i != self._hover:
            self._hover = i
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        i = self._index_at(event.position().x())
        if i >= 0:
            self.set_current(self._options[i][0])


#: What a button's `intent` property means. `quiet` recedes until hovered,
#: `danger` only shows its colour on hover so a destructive action is
#: identifiable without shouting from a resting toolbar.
INTENTS = ("default", "primary", "quiet", "danger")


def make_button(
    text: str = "",
    *,
    intent: str = "default",
    icon_name: str | None = None,
    tooltip: str = "",
    checkable: bool = False,
) -> QPushButton:
    """A toolbar button: its intent drives the style sheet, its icon is tinted."""
    if intent not in INTENTS:
        raise KeyError(f"no intent named {intent!r}")
    button = QPushButton(text)
    button.setProperty("intent", intent)
    button.setCheckable(checkable)
    if tooltip:
        button.setToolTip(tooltip)
    if icon_name:
        colour = {
            "primary": TOKENS.state.accent,
            "danger": TOKENS.state.danger,
        }.get(intent, TOKENS.ink.secondary)
        # A checked button's text goes bright under `QPushButton:checked`; its
        # icon follows to the accent through the icon's `on` state, which is
        # what the style asks for when the button is checked.
        button.setIcon(
            icon(
                icon_name,
                colour,
                disabled=TOKENS.ink.muted,
                on=TOKENS.state.accent if checkable else None,
            )
        )
    return button


class Toolbar(QWidget):
    """A control row where the row decides the widths, not the labels.

    Buttons in a group share a width (the widest in that group), groups are
    divided by a rule, and `add_stretch()` pushes what follows to the right.
    That is the whole of item 5: `Spectrum` / `Save segment` / `Discard` /
    `Delete segment` were four different widths in one undifferentiated run.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        m = TOKENS.metric
        self._layout.setContentsMargins(m.sm, m.xs, m.sm, m.xs)
        self._layout.setSpacing(m.sm)
        self._groups: list[tuple[list[QWidget], bool]] = []

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        """Never claim a width — a toolbar squeezes with its pane.

        `theme.SqueezableWidget` exists for exactly this: a control row that
        insists on its natural width raises the left pane's minimum, the
        splitter squeezes the right panel to its floor, and the splitter then
        remembers the squeeze (2026-09-07 and -09-08, the user's reports).
        Equalising a group's widths would reintroduce it without this.
        """
        return QSize(0, super().minimumSizeHint().height())

    def add_group(
        self,
        *widgets: QWidget,
        equal_width: bool = True,
        divided: bool = True,
    ) -> None:
        """Add a run of related controls, divided from the previous run.

        `divided=False` joins this run to the one before it without a rule —
        for a label that names the group that follows it.
        """
        if not widgets:
            return
        if self._groups and divided:
            rule = QFrame()
            rule.setFrameShape(QFrame.Shape.VLine)
            rule.setFixedWidth(1)
            rule.setStyleSheet(f"color: {TOKENS.surface.hairline.name()};")
            self._layout.addWidget(rule)
        for widget in widgets:
            self._layout.addWidget(widget)
        self._groups.append((list(widgets), equal_width))
        self.equalise()

    def equalise(self) -> None:
        """Re-level every equal-width group to the widest member's size hint.

        `add_group` calls this; a panel that later changes a member's label
        (and so its size hint) calls it again so the group does not go ragged.
        No panel does today — the transport row keeps its counts in tooltips —
        but it is the contract a labelled group relies on.
        """
        for widgets, equal_width in self._groups:
            if not equal_width or len(widgets) < 2:
                continue
            for widget in widgets:
                widget.setMinimumWidth(0)
            widest = max(w.sizeHint().width() for w in widgets)
            for widget in widgets:
                widget.setMinimumWidth(widest)

    def add_widget(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)

    def add_stretch(self) -> None:
        self._layout.addStretch(1)

    @property
    def groups(self) -> list[list[QWidget]]:
        return [list(widgets) for widgets, _ in self._groups]
