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

from .catalog import Criteria, SampleRow, SegmentRow, hit_label
from .similarity import Hit, Scores
from .theme import ACCENT, TEXT, TEXT_DIM

log = logging.getLogger(__name__)

SORT_ROLE = Qt.ItemDataRole.UserRole  # raw values, so the proxy sorts numbers as numbers
ANCHOR_ROLE = Qt.ItemDataRole.UserRole + 1   # True for the row that carries the ⚓
ANCHOR_GLYPH_WIDTH = 24                       # the click zone at the start of every row
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


class SampleTreeModel(QAbstractItemModel):
    """Samples, each with at most one sub-hit row underneath (§9.4)."""

    # The four CLAP columns left the list on 2026-09-08 (the user's steer): the
    # numbers stay on the Attributes tab, as bars and as a filter.
    COLUMNS = ("File", "Folder", "Length", "Type", "BPM", "Key", "Tags", "Hits", "Similarity", "Match")
    COL_SIMILARITY = 8
    COL_MATCH = 9

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
        self._hits: dict[int, Hit] = {}
        self._anchor: tuple[str, int] | None = None     # (kind, id): the ⚓ row (§9.2)

    # --- data in ---

    def set_rows(self, rows: list[SampleRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def set_similarity(self, scores: Scores | None) -> None:
        """The last explicit ranking (§9.6); None clears the column."""
        self.beginResetModel()
        self._similarity = scores
        self._hits = self._pick_hits()
        self.endResetModel()

    def set_match(self, scores: Scores | None) -> None:
        """The last free-text search (§5.2); None clears the column."""
        self.beginResetModel()
        self._match = scores
        self._hits = self._pick_hits()
        self.endResetModel()

    def _pick_hits(self) -> dict[int, Hit]:
        """Sub-hits follow the search while one is active, else the ranking."""
        if self._match is not None:
            return dict(self._match.hits)
        if self._similarity is not None:
            return dict(self._similarity.hits)
        return {}

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

    def hit_at(self, index: QModelIndex) -> Hit | None:
        if not self.is_hit(index):
            return None
        return self._hits.get(self.row_at(index).id)

    # --- QAbstractItemModel ---

    def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, column, _TOP)
        return self.createIndex(row, column, parent.row() + 1)

    def parent(self, index: QModelIndex = QModelIndex()) -> QModelIndex:  # type: ignore[override]
        if not index.isValid() or index.internalId() == _TOP:
            return QModelIndex()
        return self.createIndex(int(index.internalId()) - 1, 0, _TOP)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        if not parent.isValid():
            return len(self._rows)
        if parent.internalId() != _TOP:
            return 0                                   # a sub-hit has no children
        return 1 if self._rows[parent.row()].id in self._hits else 0

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        if role == Qt.ItemDataRole.ToolTipRole and section == 0:
            return (
                "⚓ at the start of a row anchors that sample (or hit) and ranks every "
                "sample against it, with the weight bars as they are (§9.2)"
            )
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        col = index.column()
        display = role == Qt.ItemDataRole.DisplayRole
        if self.is_hit(index):
            r = self.row_at(index)
            hit = self._hits.get(r.id)
            if hit is None:
                return None
            if role == Qt.ItemDataRole.ToolTipRole:
                return f"{r.filepath} @ {hit.start_ms} ms"
            if role == ANCHOR_ROLE:
                return self._anchor == ("segment", hit.segment_id)
            if role not in (Qt.ItemDataRole.DisplayRole, SORT_ROLE):
                return None
            length_s = (hit.end_ms - hit.start_ms) / 1000
            if col == 0:
                return "↳ " + hit_label(hit.start_ms, hit.end_ms, hit.window) if display else hit.start_ms
            if col == 2:
                return _fmt_seconds(length_s) if display else length_s
            if col == 3:
                return "window" if hit.window else "hit"
            if col == self.COL_SIMILARITY:
                return self._similarity_cell("segment", hit.segment_id, display)
            if col == self.COL_MATCH:
                return _score_cell(self._match, "segment", hit.segment_id, display)
            return "" if display else None
        r = self._rows[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return r.filepath
        if role == ANCHOR_ROLE:
            return self._anchor == ("sample", r.id)
        if role not in (Qt.ItemDataRole.DisplayRole, SORT_ROLE):
            return None
        if col == 0:
            return r.filename
        if col == 1:
            return r.folder
        if col == 2:
            return _fmt_seconds(r.duration_s) if display else (r.duration_s if r.duration_s is not None else -1.0)
        if col == 3:
            return r.structural_type or ""
        if col == 4:
            if display:
                return "" if r.tempo_bpm is None else f"{r.tempo_bpm:.0f}"
            return r.tempo_bpm if r.tempo_bpm is not None else -1.0
        if col == 5:
            return r.key or ""
        if col == 6:
            return r.tags
        if col == 7:
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
        self._anchor = item
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self._rows) - 1, self.COL_SIMILARITY),
                [ANCHOR_ROLE, SORT_ROLE],
            )

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
    """Sorting on raw values plus the Attributes tab's criteria (§9.5) and
    the quick substring filter. Criteria apply to samples; a sub-hit row is
    shown whenever its parent is."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(-1)
        self._criteria = Criteria()
        self._axis_lookup: AxisLookup = lambda _sample_id: None

    def set_criteria(self, criteria: Criteria) -> None:
        self._before_filter_change()
        self._criteria = criteria
        self._after_filter_change()

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
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        if source_parent.isValid():
            return True
        if not super().filterAcceptsRow(source_row, source_parent):
            return False
        model = self.sourceModel()
        row = model.row_at(model.index(source_row, 0))
        return self._criteria.accepts(row, self._axis_lookup(row.id))

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
    """A small anchor drawn with the pen — a text glyph would come out as a
    colour emoji on Windows and ignore it: ring, stem, crossbar, flukes."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(colour, 2.0 if bold else 1.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    cx, cy = zone.center().x(), zone.center().y()
    h = min(zone.height(), 18.0) - 5.0
    top = cy - h / 2
    r = h * 0.14
    painter.drawEllipse(QPointF(cx, top + r), r, r)
    painter.drawLine(QPointF(cx, top + 2 * r), QPointF(cx, top + h))
    painter.drawLine(QPointF(cx - h * 0.3, top + h * 0.45), QPointF(cx + h * 0.3, top + h * 0.45))
    painter.drawArc(QRectF(cx - h * 0.45, top + h * 0.3, h * 0.9, h * 0.7), 200 * 16, 140 * 16)
    painter.restore()


class AnchorDelegate(QStyledItemDelegate):
    """The ⚓ at the start of every row of the list (2026-09-08, the user's
    steer: anchoring and ranking should be one click on the row, not a
    button, a tab and a step). Paints the File cell shifted right by a click
    zone that shows the glyph — accent for the row that is the anchor, dim
    otherwise, brighter under the mouse — and turns a click in that zone
    into `anchor_clicked(index)`; the rest of the cell behaves as before."""

    anchor_clicked = Signal(QModelIndex)

    def paint(self, painter, option, index) -> None:  # noqa: N802
        zone = QRectF(option.rect.left(), option.rect.top(), ANCHOR_GLYPH_WIDTH, option.rect.height())
        full = QStyleOptionViewItem(option)
        self.initStyleOption(full, index)
        full.text = ""
        style = full.widget.style() if full.widget is not None else QStyle()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, full, painter, full.widget)
        shifted = QStyleOptionViewItem(option)
        shifted.rect = option.rect.adjusted(ANCHOR_GLYPH_WIDTH, 0, 0, 0)
        super().paint(painter, shifted, index)
        anchored = bool(index.data(ANCHOR_ROLE))
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        _paint_anchor(painter, zone, ACCENT if anchored else (TEXT if hovered else TEXT_DIM), bold=anchored)

    def sizeHint(self, option, index):  # noqa: N802
        size = super().sizeHint(option, index)
        size.setWidth(size.width() + ANCHOR_GLYPH_WIDTH)
        return size

    def editorEvent(self, event, model, option, index) -> bool:  # noqa: N802
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
            and event.position().x() < option.rect.left() + ANCHOR_GLYPH_WIDTH
        ):
            self.anchor_clicked.emit(index)
            return True
        return super().editorEvent(event, model, option, index)
