"""Per-column filters in the list's header (2026-09-08, the user's steer:
"the quick filter should be available at each column header").

A click on a header section opens a popup under it — roughly Excel's
AutoFilter: *Sort ascending* / *Sort descending*, then the column's own
editor, then *Clear*. The editor is a text field for File, Folder and Tags
("contains"), a checklist of the values present for Type and Key, and a
min/max pair for Length, BPM, Hits, Similarity and Match. Filters apply as
you type and a filtered section carries a dot. A header click no longer
sorts by itself (the popup's buttons do), so the view's own sorting is off
and the header keeps its indicator where the last sort put it. Filters are
view state (§9.4): nothing here touches the index.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
)
from PySide6.QtWidgets import QApplication

from .listmodel import ColumnFilter
from .theme import ACCENT, TEXT, TEXT_DIM

NONE_LABEL = "(none)"      # how an empty cell reads in a checklist


@dataclass(frozen=True)
class ColumnSpec:
    """What a column's popup edits."""

    kind: str                 # "text" | "values" | "range"
    unit: str = ""            # after a range box
    decimals: int = 0
    maximum: float = 1e9
    scale: float = 1.0        # shown units per raw unit (Similarity, Match: raw 0..1 shown as 0..100)


class FilterPopup(QFrame):
    """One column's popup: sort buttons, the editor for its kind, Clear."""

    filter_changed = Signal(int, object)     # column, ColumnFilter | None
    sort_requested = Signal(int, object)     # column, Qt.SortOrder

    def __init__(
        self,
        column: int,
        title: str,
        spec: ColumnSpec,
        current: ColumnFilter | None,
        values: list[str] = (),
        parent=None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("filterPopup")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._column = column
        self._spec = spec
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        head = QLabel(title)
        head.setStyleSheet("font-weight: bold;")
        layout.addWidget(head)

        sort_row = QHBoxLayout()
        ascending = QPushButton("↑ Sort ascending")
        descending = QPushButton("↓ Sort descending")
        ascending.clicked.connect(lambda: self._sort(Qt.SortOrder.AscendingOrder))
        descending.clicked.connect(lambda: self._sort(Qt.SortOrder.DescendingOrder))
        sort_row.addWidget(ascending)
        sort_row.addWidget(descending)
        layout.addLayout(sort_row)

        self._edit: QLineEdit | None = None
        self._list: QListWidget | None = None
        self._low: QDoubleSpinBox | None = None
        self._high: QDoubleSpinBox | None = None
        if spec.kind == "text":
            self._edit = QLineEdit(current.text if current else "")
            self._edit.setPlaceholderText("contains…")
            self._edit.setClearButtonEnabled(True)
            self._edit.textChanged.connect(self._emit)
            layout.addWidget(self._edit)
        elif spec.kind == "values":
            self._list = QListWidget()
            self._list.setMaximumHeight(220)
            accepted = None if current is None or current.values is None else current.values
            for value in values:
                item = QListWidgetItem(value or NONE_LABEL)
                item.setData(Qt.ItemDataRole.UserRole, value)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if accepted is None or value in accepted else Qt.CheckState.Unchecked
                )
                self._list.addItem(item)
            self._list.itemChanged.connect(self._emit)
            layout.addWidget(self._list)
            all_none = QHBoxLayout()
            every = QPushButton("All")
            none = QPushButton("None")
            every.clicked.connect(lambda: self._check_all(True))
            none.clicked.connect(lambda: self._check_all(False))
            all_none.addWidget(every)
            all_none.addWidget(none)
            all_none.addStretch(1)
            layout.addLayout(all_none)
        else:
            self._low = self._range_box(spec, None if current is None else current.low)
            self._high = self._range_box(spec, None if current is None else current.high)
            row = QHBoxLayout()
            row.addWidget(QLabel("from"))
            row.addWidget(self._low)
            row.addWidget(QLabel("to"))
            row.addWidget(self._high)
            row.addStretch(1)
            layout.addLayout(row)

        clear = QPushButton("Clear filter")
        clear.clicked.connect(self._clear)
        layout.addWidget(clear)

    def _range_box(self, spec: ColumnSpec, raw: float | None) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setDecimals(spec.decimals)
        box.setRange(0.0, spec.maximum)
        box.setSpecialValueText("any")
        if spec.unit:
            box.setSuffix(f" {spec.unit}")
        box.setValue(0.0 if raw is None else raw * spec.scale)
        box.valueChanged.connect(self._emit)
        return box

    # --- state out ---

    def current_filter(self) -> ColumnFilter | None:
        """The editor as a filter; None when it accepts everything."""
        spec = self._spec
        if self._edit is not None:
            f = ColumnFilter(text=self._edit.text().strip())
        elif self._list is not None:
            items = [self._list.item(i) for i in range(self._list.count())]
            checked = [it.data(Qt.ItemDataRole.UserRole) for it in items if it.checkState() == Qt.CheckState.Checked]
            f = ColumnFilter() if len(checked) == len(items) else ColumnFilter(values=frozenset(checked))
        else:
            low = self._low.value() / spec.scale if self._low.value() > 0 else None
            high = self._high.value() / spec.scale if self._high.value() > 0 else None
            f = ColumnFilter(low=low, high=high)
        return f if f.active else None

    # --- plumbing ---

    def _emit(self, *_args) -> None:
        self.filter_changed.emit(self._column, self.current_filter())

    def _check_all(self, checked: bool) -> None:
        assert self._list is not None
        self._list.blockSignals(True)
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self._list.blockSignals(False)
        self._emit()

    def _clear(self) -> None:
        if self._edit is not None:
            self._edit.clear()
        elif self._list is not None:
            self._check_all(True)
        else:
            self._low.setValue(0.0)
            self._high.setValue(0.0)
        self.filter_changed.emit(self._column, None)
        self.close()

    def _sort(self, order: Qt.SortOrder) -> None:
        self.sort_requested.emit(self._column, order)
        self.close()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.close()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._edit is not None:
            self._edit.setFocus()


class FilterHeader(QHeaderView):
    """The list's horizontal header: a click on a section opens its filter
    popup; a filtered section carries a dot. `specs` says what each column
    edits; a column without a spec has no popup.

    Configurable since 2026-09-09 (the user's steer): sections drag to a new
    place; a right-click opens a menu to *replace* the column with a hidden
    one, show or hide any column, or reset; `fixed` names the columns the
    view manages itself (the score columns) — they are left out of the menu.
    `layout_changed` fires on every move, resize, show or hide, so the window
    can save `saveState()` at once."""

    filter_changed = Signal(int, object)     # column, ColumnFilter | None
    sort_requested = Signal(int, object)     # column, Qt.SortOrder
    layout_changed = Signal()                # moved, resized, shown, hidden or reset
    tree_clicked = Signal()                  # the expander column's header cell: expand / collapse all

    def __init__(
        self, specs: dict[int, ColumnSpec], parent=None, fixed: Iterable[int] = (),
        pinned: dict[int, int] | None = None, tree_column: int = -1, anchor_column: int = -1,
    ) -> None:
        """`fixed`: columns the menu leaves alone. `pinned`: {column: width} —
        the columns nailed to the left edge, unmovable and unresizable (the
        expander and the anchor); `tree_column` is the one whose header cell
        toggles the sections, `anchor_column` the one whose header shows a ring."""
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._specs = dict(specs)
        self._pinned = dict(pinned or {})
        self._fixed = set(fixed) | set(self._pinned)
        self._tree_column = tree_column
        self._anchor_column = anchor_column
        self._sections_open = False
        self._filters: dict[int, ColumnFilter] = {}
        self._popup: FilterPopup | None = None
        self._before: tuple[int, Qt.SortOrder] | None = None
        self._press_pos: QPointF | None = None
        self._dragged = False
        self._default_state = None
        self.setSectionsClickable(True)
        self.setSortIndicatorShown(True)
        self.setSectionsMovable(True)
        self.sectionClicked.connect(self.open_filter)
        self.sectionMoved.connect(self._on_section_moved)
        self.sectionResized.connect(lambda *_: self.layout_changed.emit())

    def install_on(self, view) -> None:
        """Make this the view's header. The view resets clickability when it
        adopts a header (and again on setSortingEnabled), so it is re-asserted here."""
        view.setHeader(self)
        self.setSectionsClickable(True)
        self.setSortIndicatorShown(True)
        self.setSectionsMovable(True)
        for column, width in self._pinned.items():
            self.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.resizeSection(column, width)

    # --- a click opens the popup; the indicator stays where the last sort put it ---

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._before = (self.sortIndicatorSection(), self.sortIndicatorOrder())
        self._press_pos = event.position()
        self._dragged = False
        if self.logicalIndexAt(event.position().toPoint()) in self._pinned:
            self.setSectionsMovable(False)          # a pinned column never starts a drag
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press_pos is not None and not self._dragged:
            if (event.position() - self._press_pos).manhattanLength() >= QApplication.startDragDistance():
                self._dragged = True                # a drag moves the section; it is not a click
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)          # flips the indicator on a click: put it back
        self.setSectionsMovable(True)
        if self._before is not None and (self.sortIndicatorSection(), self.sortIndicatorOrder()) != self._before:
            self.setSortIndicator(*self._before)
        self._before = None

    def open_filter(self, column: int) -> None:
        if self._dragged:
            return
        if column == self._tree_column:             # the expander column's header: expand / collapse all
            self.tree_clicked.emit()
            return
        spec = self._specs.get(column)
        model = self.model()
        if spec is None or model is None:
            return
        title = str(model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) or "")
        values: list[str] = []
        if spec.kind == "values":
            source = model.sourceModel() if hasattr(model, "sourceModel") else model
            if hasattr(source, "distinct_values"):
                values = source.distinct_values(column)
        popup = FilterPopup(column, title, spec, self._filters.get(column), values, self)
        popup.filter_changed.connect(self._on_filter)
        popup.sort_requested.connect(self.sort_requested)
        popup.move(self.mapToGlobal(QPoint(self.sectionViewportPosition(column), self.height())))
        popup.show()
        self._popup = popup

    @property
    def popup(self) -> FilterPopup | None:
        return self._popup if self._popup is not None and self._popup.isVisible() else None

    # --- state ---

    def set_filter(self, column: int, column_filter: ColumnFilter | None) -> None:
        if column_filter is None or not column_filter.active:
            self._filters.pop(column, None)
        else:
            self._filters[column] = column_filter
        self.updateSection(column)

    @property
    def filters(self) -> dict[int, ColumnFilter]:
        return dict(self._filters)

    def _on_filter(self, column: int, column_filter: ColumnFilter | None) -> None:
        self.set_filter(column, column_filter)
        self.filter_changed.emit(column, column_filter)

    # --- the columns: order, visibility (2026-09-09) ---

    def remember_default(self) -> None:
        """The arrangement *Reset columns* goes back to (called once the view
        has laid the defaults out)."""
        self._default_state = self.saveState()

    def _names(self) -> dict[int, str]:
        model = self.model()
        return {
            c: str(model.headerData(c, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) or c)
            for c in range(self.count())
        } if model is not None else {}

    def _on_section_moved(self, logical: int, old_visual: int, new_visual: int) -> None:
        """A pinned column stays where it is, and nothing lands among the pinned
        ones: such a move is undone before it is saved."""
        if logical in self._pinned or new_visual < len(self._pinned):
            self.blockSignals(True)
            try:
                self.moveSection(new_visual, old_visual)
            finally:
                self.blockSignals(False)
            return
        self.layout_changed.emit()

    @property
    def sections_open(self) -> bool:
        return self._sections_open

    def set_sections_open(self, open_: bool) -> None:
        """What the expander column's header cell shows (▾ open, ▸ folded)."""
        self._sections_open = bool(open_)
        if self._tree_column >= 0:
            self.updateSection(self._tree_column)

    def hidden_columns(self) -> list[int]:
        """The columns a right-click can bring back, in logical order."""
        return [c for c in range(self.count()) if c not in self._fixed and self.isSectionHidden(c)]

    def first_visible_column(self) -> int:
        """The first movable column shown — where a section row's label goes."""
        for visual in range(self.count()):
            logical = self.logicalIndex(visual)
            if logical >= 0 and logical not in self._pinned and not self.isSectionHidden(logical):
                return logical
        return 0

    def set_column_visible(self, column: int, visible: bool) -> None:
        """Show or hide a column — never the last one shown."""
        if column in self._fixed:
            return
        shown = [c for c in range(self.count()) if c not in self._fixed and not self.isSectionHidden(c)]
        if not visible and shown == [column]:
            return
        self.setSectionHidden(column, not visible)
        self.layout_changed.emit()

    def replace_column(self, old: int, new: int) -> None:
        """*Replace with*: `new` takes `old`'s place and `old` goes."""
        if old == new or old in self._fixed or new in self._fixed:
            return
        position = self.visualIndex(old)
        self.setSectionHidden(new, False)
        self.moveSection(self.visualIndex(new), position)
        self.setSectionHidden(old, True)
        self.layout_changed.emit()

    def reset_columns(self) -> None:
        if self._default_state is not None:
            self.restoreState(self._default_state)
            self.layout_changed.emit()

    def column_menu(self, section: int) -> QMenu:
        """The right-click menu on a section: replace it with a hidden column,
        tick columns on or off, reset."""
        names = self._names()
        menu = QMenu(self)
        if section >= 0 and section not in self._fixed:
            replace = menu.addMenu(f"Replace “{names.get(section, section)}” with")
            hidden = self.hidden_columns()
            for column in hidden:
                action = replace.addAction(names.get(column, str(column)))
                action.triggered.connect(lambda _checked=False, a=section, b=column: self.replace_column(a, b))
            replace.setEnabled(bool(hidden))
            menu.addSeparator()
        for column in range(self.count()):
            if column in self._fixed:
                continue
            action = menu.addAction(names.get(column, str(column)))
            action.setCheckable(True)
            action.setChecked(not self.isSectionHidden(column))
            action.triggered.connect(lambda on, c=column: self.set_column_visible(c, bool(on)))
        menu.addSeparator()
        reset = menu.addAction("Reset columns")
        reset.triggered.connect(self.reset_columns)
        return menu

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self.column_menu(self.logicalIndexAt(event.pos())).exec(event.globalPos())

    # --- painting ---

    def paintSection(self, painter: QPainter, rect, logical_index: int) -> None:  # noqa: N802
        painter.save()
        super().paintSection(painter, rect, logical_index)
        painter.restore()
        if logical_index == self._tree_column:
            painter.save()
            painter.setPen(TEXT)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "▾" if self._sections_open else "▸")
            painter.restore()
        elif logical_index == self._anchor_column:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(TEXT_DIM, 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(rect.center().x(), rect.center().y()), 4.0, 4.0)
            painter.restore()
        if logical_index in self._filters:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ACCENT)
            painter.drawEllipse(QPointF(rect.right() - 7.0, rect.top() + 7.0), 3.5, 3.5)
            painter.restore()
