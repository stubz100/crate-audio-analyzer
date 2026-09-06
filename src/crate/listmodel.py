"""Qt table models for the "listen and grab" list (Phase 4.5, spec §12).

Thin adapters over `catalog` rows. The one piece of behaviour that lives
here is **drag-out** (spec §11): `mimeData` hands the OS real file URLs —
Windows turns those into CF_HDROP, so a drop into Bitwig behaves exactly like
a drag from Explorer. A segment is rendered on first drag (spec §9.2 /
§6.5) through the `render` callable the segment model is given.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, Qt, QUrl

from .catalog import SampleRow, SegmentRow

log = logging.getLogger(__name__)

SORT_ROLE = Qt.ItemDataRole.UserRole  # raw values, so the proxy sorts numbers as numbers


def _fmt_seconds(value: float | None) -> str:
    return "" if value is None else f"{value:.2f} s"


class SampleTableModel(QAbstractTableModel):
    """One row per sample — never per segment (§6.4)."""

    COLUMNS = ("File", "Folder", "Length", "Type", "Class", "BPM", "Key", "Tags", "Hits")

    def __init__(self, rows: list[SampleRow] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[SampleRow] = list(rows or [])

    def set_rows(self, rows: list[SampleRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, index: QModelIndex) -> SampleRow:
        return self._rows[index.row()]

    # --- QAbstractTableModel ---

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.ToolTipRole:
            return r.filepath
        if role not in (Qt.ItemDataRole.DisplayRole, SORT_ROLE):
            return None
        display = role == Qt.ItemDataRole.DisplayRole
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
        return None

    def flags(self, index: QModelIndex):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return base | Qt.ItemFlag.ItemIsDragEnabled if index.isValid() else base

    def supportedDragActions(self):  # noqa: N802
        return Qt.DropAction.CopyAction

    def mimeData(self, indexes) -> QMimeData:  # noqa: N802
        paths: list[str] = []
        for index in indexes:
            path = self._rows[index.row()].filepath
            if path not in paths:
                paths.append(path)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        return mime


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
