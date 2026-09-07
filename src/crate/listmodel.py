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
    QMimeData,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QUrl,
)

from .catalog import Criteria, SampleRow, SegmentRow
from .similarity import Hit, Scores

log = logging.getLogger(__name__)

SORT_ROLE = Qt.ItemDataRole.UserRole  # raw values, so the proxy sorts numbers as numbers
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

    COLUMNS = (
        "File", "Folder", "Length", "Type", "Class", "BPM", "Key", "Tags", "Hits",
        "Similarity", "Match",
    )
    COL_SIMILARITY = 9
    COL_MATCH = 10

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
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
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
            if role not in (Qt.ItemDataRole.DisplayRole, SORT_ROLE):
                return None
            length_s = (hit.end_ms - hit.start_ms) / 1000
            if col == 0:
                return (
                    f"↳ hit @ {hit.start_ms / 1000:.3f} s ({hit.end_ms - hit.start_ms} ms)"
                    if display else hit.start_ms
                )
            if col == 2:
                return _fmt_seconds(length_s) if display else length_s
            if col == 3:
                return "hit"
            if col == self.COL_SIMILARITY:
                return _score_cell(self._similarity, "segment", hit.segment_id, display)
            if col == self.COL_MATCH:
                return _score_cell(self._match, "segment", hit.segment_id, display)
            return "" if display else None
        r = self._rows[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return r.filepath
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
            if r.content_class:
                return r.content_class
            return "?" if (display and r.confidence is not None) else ""
        if col == 5:
            if display:
                return "" if r.tempo_bpm is None else f"{r.tempo_bpm:.0f}"
            return r.tempo_bpm if r.tempo_bpm is not None else -1.0
        if col == 6:
            return r.key or ""
        if col == 7:
            return r.tags
        if col == 8:
            if display:
                if not r.segment_count:
                    return ""
                return f"{r.segment_count}!" if r.flagged_segments else str(r.segment_count)
            return r.segment_count
        if col == self.COL_SIMILARITY:
            return _score_cell(self._similarity, "sample", r.id, display)
        if col == self.COL_MATCH:
            return _score_cell(self._match, "sample", r.id, display)
        return None

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
