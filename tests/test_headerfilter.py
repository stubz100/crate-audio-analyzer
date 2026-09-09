"""Per-column filters in the list header (2026-09-08): the filter itself,
the proxy, the popup for each kind, and the header that opens it — offscreen."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTreeView

from crate.catalog import SampleRow
from crate.headerfilter import ColumnSpec, FilterHeader, FilterPopup
from crate.listmodel import ColumnFilter, ListProxy, SampleTreeModel
from crate.tagbars import TagBars


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _row(i: int, name: str, kind: str, seconds: float, bpm: float | None, key: str | None, tags: str, hits: int) -> SampleRow:
    return SampleRow(i, f"C:/lib/{name}", name, "lib", seconds, kind, None, None, bpm, key, tags, hits, 0)


ROWS = [
    _row(1, "kick.wav", "one-shot", 0.4, None, None, "kick drum, percussion", 0),
    _row(2, "loop.wav", "loop", 4.0, 120.0, "Am", "drum loop, percussion", 6),
    _row(3, "pad.wav", "multi-hit", 12.0, None, "C", "synth pad, strings", 3),
]

C = SampleTreeModel
SPECS = {
    C.COL_FOLDER: ColumnSpec("text"), C.COL_FILE: ColumnSpec("text"),
    C.COL_LENGTH: ColumnSpec("range", "s", decimals=2, maximum=99_999), C.COL_TYPE: ColumnSpec("values"),
    C.COL_BPM: ColumnSpec("range", "BPM", maximum=999), C.COL_KEY: ColumnSpec("values"), C.COL_TAGS: ColumnSpec("text"),
    C.COL_HITS: ColumnSpec("range", maximum=9_999), C.COL_SIMILARITY: ColumnSpec("range", "%", maximum=100, scale=100.0),
}


def _view(app):
    model = SampleTreeModel(lambda _id: None)
    model.set_rows(ROWS)
    proxy = ListProxy()
    proxy.setSourceModel(model)
    view = QTreeView()
    view.setModel(proxy)
    view.setSortingEnabled(False)
    header = FilterHeader(SPECS, view, fixed=(C.COL_SIMILARITY, C.COL_MATCH),
                          pinned={C.COL_TREE: 24, C.COL_ANCHOR: 26}, tree_column=C.COL_TREE, anchor_column=C.COL_ANCHOR)
    header.install_on(view)
    header.filter_changed.connect(proxy.set_column_filter)
    header.sort_requested.connect(view.sortByColumn)
    view.resize(900, 300)
    view.show()
    QApplication.processEvents()                  # lay the header out before anything clicks it
    return model, proxy, view, header


def _names(proxy) -> list[str]:
    return [proxy.data(proxy.index(r, C.COL_FILE)) for r in range(proxy.rowCount())]


def test_column_filter_accepts():
    assert not ColumnFilter().active
    assert ColumnFilter(text="Kick").accepts("kick.wav", None) and not ColumnFilter(text="snare").accepts("kick.wav", None)
    assert ColumnFilter(values=frozenset({"loop", ""})).accepts("", None)
    assert not ColumnFilter(values=frozenset({"loop"})).accepts("one-shot", None)
    assert ColumnFilter(low=1.0).accepts("4.00 s", 4.0) and not ColumnFilter(low=1.0).accepts("0.40 s", 0.4)
    assert not ColumnFilter(low=1.0).accepts("", -1.0) and not ColumnFilter(high=100.0).accepts("", None)
    assert ColumnFilter(low=0.5, high=0.9).accepts("70", 0.7) and not ColumnFilter(high=0.6).accepts("70", 0.7)


def test_proxy_applies_column_filters_and_the_model_lists_distinct_values(app):
    model, proxy, view, header = _view(app)
    try:
        assert model.distinct_values(C.COL_TYPE) == ["loop", "multi-hit", "one-shot"]
        assert model.distinct_values(C.COL_KEY) == ["", "Am", "C"]
        proxy.set_column_filter(C.COL_TAGS, ColumnFilter(text="percussion"))
        assert _names(proxy) == ["kick.wav", "loop.wav"] and proxy.filtered_columns == {C.COL_TAGS}
        proxy.set_column_filter(C.COL_HITS, ColumnFilter(low=1))
        assert _names(proxy) == ["loop.wav"]
        proxy.set_column_filter(C.COL_TAGS, None)
        proxy.set_column_filter(C.COL_HITS, ColumnFilter())              # inactive: clears
        assert _names(proxy) == ["kick.wav", "loop.wav", "pad.wav"] and not proxy.filtered_columns
        proxy.set_column_filter(C.COL_BPM, ColumnFilter(low=100.0))      # no tempo: never in a range
        assert _names(proxy) == ["loop.wav"]
    finally:
        view.close()


def test_popups_edit_each_kind_and_the_header_opens_them(app):
    model, proxy, view, header = _view(app)
    try:
        view.sortByColumn(C.COL_LENGTH, Qt.SortOrder.DescendingOrder)
        assert header.sortIndicatorSection() == C.COL_LENGTH and _names(proxy)[0] == "pad.wav"

        # a click on a section opens its popup and leaves the sort where it was
        x = header.sectionViewportPosition(C.COL_TYPE) + 10
        QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x, 8))
        app.processEvents()
        popup = header.popup
        assert popup is not None and popup._list is not None and popup._list.count() == 3
        assert (header.sortIndicatorSection(), header.sortIndicatorOrder()) == (C.COL_LENGTH, Qt.SortOrder.DescendingOrder)
        popup._list.item(0).setCheckState(Qt.CheckState.Unchecked)         # "loop" off
        assert _names(proxy) == ["pad.wav", "kick.wav"] and C.COL_TYPE in header.filters
        assert not view.grab().isNull()                                     # the section paints its dot
        popup._check_all(True)
        assert C.COL_TYPE not in header.filters and len(_names(proxy)) == 3
        popup.close()

        # the popup's buttons sort
        header.open_filter(C.COL_FILE)
        header.popup._sort(Qt.SortOrder.AscendingOrder)
        assert _names(proxy) == ["kick.wav", "loop.wav", "pad.wav"] and header.sortIndicatorSection() == C.COL_FILE

        # text and range editors
        text = FilterPopup(C.COL_FILE, "File", SPECS[C.COL_FILE], None)
        text._edit.setText("oop")
        assert text.current_filter() == ColumnFilter(text="oop")
        text._edit.clear()
        assert text.current_filter() is None
        length = FilterPopup(C.COL_LENGTH, "Length", SPECS[C.COL_LENGTH], ColumnFilter(low=1.0))
        assert length._low.value() == 1.0 and length._high.specialValueText() == "any"
        length._high.setValue(5.0)
        assert length.current_filter() == ColumnFilter(low=1.0, high=5.0)
        similarity = FilterPopup(SampleTreeModel.COL_SIMILARITY, "Similarity", SPECS[SampleTreeModel.COL_SIMILARITY], None)
        similarity._low.setValue(70)                                        # shown in %, stored raw
        assert similarity.current_filter() == ColumnFilter(low=0.7)
        cleared: list = []
        similarity.filter_changed.connect(lambda col, f: cleared.append((col, f)))
        similarity._clear()
        assert cleared[-1] == (SampleTreeModel.COL_SIMILARITY, None)
    finally:
        view.close()


def test_tag_bars_show_and_click(app):
    bars = TagBars()
    bars.resize(1000, 90)
    clicked: list[str] = []
    bars.tag_clicked.connect(clicked.append)
    assert bars.slot_at(10) == -1 and not bars.grab().isNull()
    bars.show_tags([("vocal", 0.61), ("brass", 0.38), ("vocal chop", 0.34)])
    assert len(bars.tags) == 3 and bars.slot_at(10) == 0 and bars.slot_at(500) == 1 and bars.slot_at(999) == 2
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent
    pos = QPointF(500.0, 40.0)
    bars.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, pos, pos, Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    assert "brass" in bars.toolTip()
    bars.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert clicked == ["brass"]
    assert not bars.grab().isNull()
    bars.show_tags([(f"t{i}", 0.1 * i) for i in range(15)])
    assert len(bars.tags) == 10


def test_a_drag_moves_a_section_without_opening_its_popup(app):
    model, proxy, view, header = _view(app)
    try:
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent

        def mouse(kind, x):
            pos = QPointF(x, 8.0)
            button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
            return QMouseEvent(kind, pos, header.viewport().mapToGlobal(pos.toPoint()), button,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)

        assert header.sectionsMovable()
        before = header.visualIndex(C.COL_TYPE)
        x0 = header.sectionViewportPosition(C.COL_TYPE) + 10
        x1 = header.sectionViewportPosition(C.COL_TAGS) + 10
        header.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, x0))
        for x in range(int(x0), int(x1), 8):
            header.mouseMoveEvent(mouse(QEvent.Type.MouseMove, x))
        header.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, x1))
        app.processEvents()
        assert header.popup is None                                        # a drag is not a click
        assert header.visualIndex(C.COL_TYPE) != before                    # the section moved
        assert header.first_visible_column() == C.COL_FOLDER               # the first movable column
        header.set_column_visible(C.COL_FOLDER, False)
        assert header.first_visible_column() == C.COL_FILE
        header.set_sections_open(True)
        assert header.sections_open and not view.grab().isNull()           # the ▾ and the ring paint
        toggles: list[int] = []
        header.tree_clicked.connect(lambda: toggles.append(1))
        header._dragged = False                                            # a fresh click, not the drag above
        header.open_filter(C.COL_TREE)
        assert toggles == [1] and header.popup is None                     # the expander's header cell toggles, no popup
    finally:
        view.close()
