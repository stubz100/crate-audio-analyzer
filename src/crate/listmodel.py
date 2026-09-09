"""Qt models for the list view (Phase 4.5, extended in Phase 7 — spec §9.4).

Thin adapters over `catalog` rows and `similarity` scores. `SampleTreeModel`
is two levels deep: samples at the top, and under a sample at most one
indented **sub-hit** row — the segment that is the actual best match for the
current ranking or search (§9.4 "hit within"). Segments never appear as
top-level rows (§6.4). `ListProxy` sorts on raw values and applies the
Attributes tab's criteria to samples only; a sub-hit follows its parent.

The one piece of behaviour that lives here is **drag-out** (spec §11):
`mimeData` hands the OS real file URLs — Windows turns those into CF_HDROP,
so a drop into Bitwig behaves exactly like a drag from Explorer. A segment
(a sub-hit, or a row of the drill-down table) is rendered on first drag
(§6.5) through the `render` callable the models are given.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractTableModel,
    QEvent,
    QMimeData,
    QModelIndex,
    QPointF,
    QRectF,
    QSortFilterProxyModel,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from .catalog import Criteria, SampleRow, Section, SegmentRow, hit_label
from .similarity import Hit, Scores
from .theme import ACCENT, TEXT, TEXT_DIM

log = logging.getLogger(__name__)

SORT_ROLE = Qt.ItemDataRole.UserRole  # raw values, so the proxy sorts numbers as numbers
ANCHOR_ROLE = Qt.ItemDataRole.UserRole + 1   # True for the row that carries the anchor circle
SECTION_LABEL_ROLE = Qt.ItemDataRole.UserRole + 2   # a section row's "↳ hit @ …": the delegate draws it in the first column shown
ANCHOR_COLUMN_WIDTH = 26                      # the fixed anchor column
TREE_COLUMN_WIDTH = 24                        # the fixed expander column
_TOP = 0  # internalId of a top-level index; a sub-hit carries its parent's row + 1


def _fmt_seconds(value: float | None) -> str:
    return "" if value is None else f"{value:.2f} s"


def _score_cell(scores: Scores | None, kind: str, item_id: int, display: bool):
    value = None
    if scores is not None:
        value = (scores.sample if kind == "sample" else scores.segment).get(item_id)
    if display:
        return "" if value is None else f"{value * 100:.0f}"
    return -1.0 if value is None else value


@dataclass(frozen=True)
class ColumnFilter:
    """One column's filter from the list header's popup (2026-09-08,
    `headerfilter.py`): a substring of the cell's text, a set of accepted
    cell texts, or a range on the cell's raw value (the SORT_ROLE number)."""

    text: str = ""
    values: frozenset[str] | None = None
    low: float | None = None
    high: float | None = None

    @property
    def active(self) -> bool:
        return bool(self.text) or self.values is not None or self.low is not None or self.high is not None

    def accepts(self, display, raw) -> bool:
        shown = "" if display is None else str(display)
        if self.text and self.text.lower() not in shown.lower():
            return False
        if self.values is not None and shown not in self.values:
            return False
        if self.low is not None or self.high is not None:
            if not isinstance(raw, (int, float)) or raw < 0:     # -1 = no value: never in a range
                return False
            if self.low is not None and raw < self.low:
                return False
            if self.high is not None and raw > self.high:
                return False
        return True


class SampleTreeModel(QAbstractItemModel):
    """Samples, each with every one of its sections underneath (§9.4; 2026-09-08,
    the user's steer — a sample keeps all its sections under it, and a ranking
    orders them by similarity). A CLAP window of a long file (§6.4) shows only
    while it carries a score."""

    # Two fixed columns first (2026-09-09, the user's steer): the tree expander and
    # the anchor circle — unmovable, unsortable, no filter. Then Folder, File and
    # the caption (2026-09-08); the four CLAP columns left the list earlier.
    COLUMNS = ("", "", "Folder", "File", "Caption", "Length", "Type", "BPM", "Key", "Tags", "Hits", "Similarity", "Match")
    COL_TREE, COL_ANCHOR, COL_FOLDER, COL_FILE, COL_CAPTION, COL_LENGTH, COL_TYPE, COL_BPM, COL_KEY, COL_TAGS, COL_HITS = range(11)
    COL_SIMILARITY = 11
    COL_MATCH = 12
    FIXED = (COL_TREE, COL_ANCHOR)                 # pinned at the left; the score columns are the view's to show

    def __init__(
        self,
        render: Callable[[int], Path] | None = None,
        rows: list[SampleRow] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._render = render
        self._rows: list[SampleRow] = list(rows or [])
        self._similarity: Scores | None = None
        self._match: Scores | None = None
        self._hits: dict[int, Hit] = {}                     # the winning hit per sample: badges, expansion
        self._sections: dict[int, list[Section]] = {}       # every section, by sample, in time order
        self._children: dict[int, list[Section]] = {}       # the child rows shown, in their order
        self._parent_of: dict[int, int] = {}                # segment id → sample id, for the shown rows
        self._anchor: tuple[str, int] | None = None     # (kind, id): the ⚓ row (§9.2)

    # --- data in ---

    def set_rows(self, rows: list[SampleRow], sections: Mapping[int, list[Section]] | None = None) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._sections = {k: list(v) for k, v in (sections or {}).items()}
        self._rebuild_children()
        self.endResetModel()

    def set_similarity(self, scores: Scores | None) -> None:
        """The last explicit ranking (§9.6); None clears the column."""
        self.beginResetModel()
        self._similarity = scores
        self._hits = self._pick_hits()
        self._rebuild_children()
        self.endResetModel()

    def set_match(self, scores: Scores | None) -> None:
        """The last free-text search (§5.2); None clears the column."""
        self.beginResetModel()
        self._match = scores
        self._hits = self._pick_hits()
        self._rebuild_children()
        self.endResetModel()

    def _pick_hits(self) -> dict[int, Hit]:
        """Sub-hits follow the search while one is active, else the ranking."""
        if self._match is not None:
            return dict(self._match.hits)
        if self._similarity is not None:
            return dict(self._similarity.hits)
        return {}

    def _rebuild_children(self) -> None:
        """The child rows: every segment, plus the CLAP windows that carry a
        score — by score, best first, unscored ones after in time order; in
        time order when nothing is scored."""
        scores = self._match if self._match is not None else self._similarity
        self._children = {}
        self._parent_of = {}
        for r in self._rows:
            sections = self._sections.get(r.id)
            if not sections:
                continue
            shown = []
            for section in sections:
                score = None if scores is None else scores.segment.get(section.segment_id)
                if section.window and score is None:
                    continue
                shown.append((-(score if score is not None else -1.0), section.start_ms, section))
            if not shown:
                continue
            shown.sort(key=lambda t: (t[0], t[1]))
            self._children[r.id] = [s for _, _, s in shown]
            for section in self._children[r.id]:
                self._parent_of[section.segment_id] = r.id

    def children_of(self, sample_id: int) -> list[Section]:
        return list(self._children.get(sample_id, ()))

    def hit_sample_ids(self) -> set[int]:
        """Samples whose current hit is one of their segments — the map's badges (§9.3)."""
        return set(self._hits)

    @property
    def has_similarity(self) -> bool:
        return self._similarity is not None

    @property
    def has_match(self) -> bool:
        return self._match is not None

    # --- lookups ---

    def is_hit(self, index: QModelIndex) -> bool:
        return index.isValid() and index.internalId() != _TOP

    def row_at(self, index: QModelIndex) -> SampleRow:
        """The sample of an index — a sub-hit's parent sample for a sub-hit."""
        if self.is_hit(index):
            return self._rows[int(index.internalId()) - 1]
        return self._rows[index.row()]

    def hit_at(self, index: QModelIndex) -> Section | None:
        """The section a child row shows (its parent's `row_at`)."""
        if not self.is_hit(index):
            return None
        children = self._children.get(self.row_at(index).id, ())
        return children[index.row()] if index.row() < len(children) else None

    # --- QAbstractItemModel ---

    def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:
        # Bounds checked here rather than through hasIndex(): the proxy's sort
        # asks for every index of 30k rows and hasIndex() calls back into Python
        # twice per call (2026-09-08, measured: 1.3 s of a 2.9 s anchor click).
        if row < 0 or column < 0 or column >= len(self.COLUMNS):
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, column, _TOP) if row < len(self._rows) else QModelIndex()
        if parent.internalId() != _TOP or parent.row() >= len(self._rows):
            return QModelIndex()
        if row >= len(self._children.get(self._rows[parent.row()].id, ())):
            return QModelIndex()
        return self.createIndex(row, column, parent.row() + 1)

    def parent(self, index: QModelIndex = QModelIndex()) -> QModelIndex:  # type: ignore[override]
        if not index.isValid() or index.internalId() == _TOP:
            return QModelIndex()
        return self.createIndex(int(index.internalId()) - 1, 0, _TOP)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        if not parent.isValid():
            return len(self._rows)
        if parent.internalId() != _TOP:
            return 0                                   # a section has no children
        return len(self._children.get(self._rows[parent.row()].id, ()))

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        if role == Qt.ItemDataRole.ToolTipRole and section == self.COL_TREE:
            return "Click here to expand or collapse every sample's sections."
        if role == Qt.ItemDataRole.ToolTipRole and section == self.COL_ANCHOR:
            return (
                "The anchor: the circle on a row anchors that sample (or hit) and ranks every "
                "sample against it, with the weight bars as they are (§9.2); a click on the "
                "filled circle clears the anchor."
            )
        if role == Qt.ItemDataRole.ToolTipRole:
            return "Click the header to sort this column or filter on it."
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        col = index.column()
        display = role == Qt.ItemDataRole.DisplayRole
        if self.is_hit(index):
            r = self.row_at(index)
            section = self.hit_at(index)
            if section is None:
                return None
            if role == Qt.ItemDataRole.ToolTipRole:
                return f"{r.filepath} @ {section.start_ms} ms"
            if role == ANCHOR_ROLE:
                return self._anchor == ("segment", section.segment_id)
            if role == SECTION_LABEL_ROLE:
                return "↳ " + hit_label(section.start_ms, section.end_ms, section.window)
            if role not in (Qt.ItemDataRole.DisplayRole, SORT_ROLE):
                return None
            length_s = (section.end_ms - section.start_ms) / 1000
            if col in self.FIXED:
                return "" if display else None
            if col == self.COL_FOLDER:
                return "" if display else section.start_ms   # the label is SECTION_LABEL_ROLE (drawn by the delegate)
            if col == self.COL_FILE:
                return "" if display else section.start_ms   # a name sort keeps sections in time order
            if col == self.COL_LENGTH:
                return _fmt_seconds(length_s) if display else length_s
            if col == self.COL_TYPE:
                return "window" if section.window else ("manual" if section.manual else "hit")
            if col == self.COL_SIMILARITY:
                return self._similarity_cell("segment", section.segment_id, display)
            if col == self.COL_MATCH:
                return _score_cell(self._match, "segment", section.segment_id, display)
            return "" if display else None
        r = self._rows[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return r.filepath
        if role == ANCHOR_ROLE:
            return self._anchor == ("sample", r.id)
        if role not in (Qt.ItemDataRole.DisplayRole, SORT_ROLE):
            return None
        if col in self.FIXED:
            return "" if display else None
        if col == self.COL_FOLDER:
            return r.folder
        if col == self.COL_FILE:
            return r.filename
        if col == self.COL_CAPTION:
            return r.caption
        if col == self.COL_LENGTH:
            return _fmt_seconds(r.duration_s) if display else (r.duration_s if r.duration_s is not None else -1.0)
        if col == self.COL_TYPE:
            return r.structural_type or ""
        if col == self.COL_BPM:
            if display:
                return "" if r.tempo_bpm is None else f"{r.tempo_bpm:.0f}"
            return r.tempo_bpm if r.tempo_bpm is not None else -1.0
        if col == self.COL_KEY:
            return r.key or ""
        if col == self.COL_TAGS:
            return r.tags
        if col == self.COL_HITS:
            if display:
                if not r.segment_count:
                    return ""
                return f"{r.segment_count}!" if r.flagged_segments else str(r.segment_count)
            return r.segment_count
        if col == self.COL_SIMILARITY:
            return self._similarity_cell("sample", r.id, display)
        if col == self.COL_MATCH:
            return _score_cell(self._match, "sample", r.id, display)
        return None

    def distinct_values(self, column: int) -> list[str]:
        """The cell texts a column holds across the samples, sorted — the
        header popup's checklist ('' = no value)."""
        return sorted({
            str(self.data(self.index(r, column), Qt.ItemDataRole.DisplayRole) or "")
            for r in range(len(self._rows))
        })

    def _similarity_cell(self, kind: str, item_id: int, display: bool):
        """The anchor sorts above everything, even a sample identical to it
        (both score 100): the ⚓ row is the top of the ranked list (§9.2)."""
        cell = _score_cell(self._similarity, kind, item_id, display)
        if not display and self._anchor == (kind, item_id) and cell >= 0:
            return cell + 1.0
        return cell

    def set_anchor(self, item: tuple[str, int] | None) -> None:
        """Which sample or hit carries the ⚓ (the delegate paints it)."""
        if item == self._anchor:
            return
        before, self._anchor = self._anchor, item
        # Only the rows that lost or gained the circle change (and their sort
        # key: the anchor sorts on top) — a change over the whole list made the
        # proxy re-sort 30k rows (0.5 s, measured 2026-09-08).
        roles = [ANCHOR_ROLE, SORT_ROLE]
        for old in (before, item):
            if old is None:
                continue
            kind, item_id = old
            sample_id = item_id if kind == "sample" else self._parent_of.get(item_id)
            source_row = next((i for i, r in enumerate(self._rows) if r.id == sample_id), None)
            if source_row is None:
                continue
            self.dataChanged.emit(self.index(source_row, 0), self.index(source_row, self.COL_SIMILARITY), roles)
            if kind == "segment":
                parent = self.index(source_row, 0)
                last = len(self._children.get(sample_id, ())) - 1
                if last >= 0:
                    self.dataChanged.emit(self.index(0, 0, parent), self.index(last, self.COL_SIMILARITY, parent), roles)

    @property
    def anchor(self) -> tuple[str, int] | None:
        return self._anchor

    def flags(self, index: QModelIndex):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return base | Qt.ItemFlag.ItemIsDragEnabled if index.isValid() else base

    def supportedDragActions(self):  # noqa: N802
        return Qt.DropAction.CopyAction

    def mimeData(self, indexes) -> QMimeData:  # noqa: N802
        urls: list[QUrl] = []
        seen: set[tuple[str, int]] = set()
        for index in indexes:
            if self.is_hit(index):
                hit = self.hit_at(index)
                if hit is None or ("segment", hit.segment_id) in seen:
                    continue
                seen.add(("segment", hit.segment_id))
                if self._render is None:
                    continue
                try:
                    urls.append(QUrl.fromLocalFile(str(self._render(hit.segment_id))))
                except (LookupError, ValueError, OSError) as exc:
                    log.warning("segment %d could not be rendered for drag: %s", hit.segment_id, exc)
                continue
            r = self._rows[index.row()]
            if ("sample", r.id) in seen:
                continue
            seen.add(("sample", r.id))
            urls.append(QUrl.fromLocalFile(r.filepath))
        mime = QMimeData()
        mime.setUrls(urls)
        return mime


AxisLookup = Callable[[int], Mapping[str, float] | None]


class ListProxy(QSortFilterProxyModel):
    """Sorting on raw values plus the Search tab's criteria (§9.5) and the
    list header's per-column filters (2026-09-08). Both apply to samples; a
    sub-hit row is shown whenever its parent is."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(-1)
        self._criteria = Criteria()
        self._axis_lookup: AxisLookup = lambda _sample_id: None
        self._column_filters: dict[int, ColumnFilter] = {}

    def set_criteria(self, criteria: Criteria) -> None:
        self._before_filter_change()
        self._criteria = criteria
        self._after_filter_change()

    def set_column_filter(self, column: int, column_filter: ColumnFilter | None) -> None:
        """One column's filter from the header popup; None (or an inactive
        one) clears it."""
        self._before_filter_change()
        if column_filter is None or not column_filter.active:
            self._column_filters.pop(column, None)
        else:
            self._column_filters[column] = column_filter
        self._after_filter_change()

    def column_filter(self, column: int) -> ColumnFilter | None:
        return self._column_filters.get(column)

    @property
    def filtered_columns(self) -> frozenset[int]:
        return frozenset(self._column_filters)

    def set_axis_lookup(self, lookup: AxisLookup) -> None:
        """Per-sample distance-from-anchor by axis (§9.5); reset with a
        lookup returning None when there is no anchor."""
        self._before_filter_change()
        self._axis_lookup = lookup
        self._after_filter_change()

    # Qt 6.10 replaced invalidateFilter() with beginFilterChange() /
    # endFilterChange(); older PySide6 builds only have the old call.
    def _before_filter_change(self) -> None:
        begin = getattr(self, "beginFilterChange", None)
        if begin is not None:
            begin()

    def _after_filter_change(self) -> None:
        end = getattr(self, "endFilterChange", None)
        if end is not None:
            end()
        else:
            self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        if source_parent.isValid():
            return True
        if not super().filterAcceptsRow(source_row, source_parent):
            return False
        model = self.sourceModel()
        row = model.row_at(model.index(source_row, 0))
        if not self._criteria.accepts(row, self._axis_lookup(row.id)):
            return False
        for column, column_filter in self._column_filters.items():
            index = model.index(source_row, column)
            if not column_filter.accepts(model.data(index, Qt.ItemDataRole.DisplayRole), model.data(index, SORT_ROLE)):
                return False
        return True

    def visible_sample_ids(self) -> set[int]:
        model = self.sourceModel()
        return {
            model.row_at(self.mapToSource(self.index(r, 0))).id
            for r in range(self.rowCount())
        }


class SegmentTableModel(QAbstractTableModel):
    """The selected sample's segments (the §6.4 drill-down). Dragging one
    renders it first through `render(segment_id) -> Path`."""

    COLUMNS = ("Start", "Length", "Strength", "Source", "Review")

    def __init__(self, render: Callable[[int], Path], parent=None) -> None:
        super().__init__(parent)
        self._render = render
        self._rows: list[SegmentRow] = []

    def set_rows(self, rows: list[SegmentRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, index: QModelIndex) -> SegmentRow:
        return self._rows[index.row()]

    def index_of(self, segment_id: int) -> int | None:
        for i, row in enumerate(self._rows):
            if row.id == segment_id:
                return i
        return None

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole, SORT_ROLE):
            return None
        r = self._rows[index.row()]
        col = index.column()
        display = role == Qt.ItemDataRole.DisplayRole
        if col == 0:
            return f"{r.start_ms / 1000:.3f} s" if display else r.start_ms
        if col == 1:
            return f"{r.length_ms} ms" if display else r.length_ms
        if col == 2:
            if r.strength is None:
                return "" if display else -1.0
            return f"{r.strength:.2f}" if display else r.strength
        if col == 3:
            return r.detection_method
        if col == 4:
            return ("needs review" if r.needs_review else "") if display else r.needs_review
        return None

    def flags(self, index: QModelIndex):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return base | Qt.ItemFlag.ItemIsDragEnabled if index.isValid() else base

    def supportedDragActions(self):  # noqa: N802
        return Qt.DropAction.CopyAction

    def mimeData(self, indexes) -> QMimeData:  # noqa: N802
        urls: list[QUrl] = []
        seen: set[int] = set()
        for index in indexes:
            seg = self._rows[index.row()]
            if seg.id in seen:
                continue
            seen.add(seg.id)
            try:
                urls.append(QUrl.fromLocalFile(str(self._render(seg.id))))
            except (LookupError, ValueError, OSError) as exc:
                log.warning("segment %d could not be rendered for drag: %s", seg.id, exc)
        mime = QMimeData()
        mime.setUrls(urls)
        return mime


def _paint_anchor(painter: QPainter, zone: QRectF, colour, bold: bool = False) -> None:
    """A radio-button-like circle (2026-09-08, the user's steer — the drawn
    anchor "looked awful"): an empty ring on a row that could be the anchor,
    a filled dot inside it on the row that is."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(colour, 1.4))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    centre = QPointF(zone.center().x(), zone.center().y())
    r = min(zone.height(), 18.0) * 0.28
    painter.drawEllipse(centre, r, r)
    if bold:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawEllipse(centre, r * 0.55, r * 0.55)
    painter.restore()


class AnchorDelegate(QStyledItemDelegate):
    """The list's delegate (2026-09-08/09, the user's steer). In the fixed
    **anchor column** it paints the circle — accent and filled for the row
    that is the anchor, dim otherwise, brighter under the mouse — and turns a
    click there into `anchor_clicked(index)`. In the **first movable column
    shown** (`first_column()`, whichever the user dragged there) it draws a
    section row's label. Every other cell paints as usual."""

    anchor_clicked = Signal(QModelIndex)

    def __init__(self, parent=None, first_column: Callable[[], int] | None = None, anchor_column: int = 1) -> None:
        super().__init__(parent)
        self._first = first_column or (lambda: 0)
        self._anchor_column = anchor_column

    def paint(self, painter, option, index) -> None:  # noqa: N802
        column = index.column()
        if column == self._anchor_column:
            full = QStyleOptionViewItem(option)
            self.initStyleOption(full, index)
            full.text = ""
            style = full.widget.style() if full.widget is not None else QStyle()
            style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, full, painter, full.widget)
            anchored = bool(index.data(ANCHOR_ROLE))
            hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
            _paint_anchor(painter, QRectF(option.rect), ACCENT if anchored else (TEXT if hovered else TEXT_DIM), bold=anchored)
            return
        label = index.data(SECTION_LABEL_ROLE) if column == self._first() else None
        if label:                                       # a section row: its label lives in the first movable column
            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            opt.rect = option.rect
            opt.text = str(label)
            style = opt.widget.style() if opt.widget is not None else QStyle()
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
            return
        super().paint(painter, option, index)

    def editorEvent(self, event, model, option, index) -> bool:  # noqa: N802
        if (
            index.column() == self._anchor_column
            and event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self.anchor_clicked.emit(index)
            return True
        return super().editorEvent(event, model, option, index)
