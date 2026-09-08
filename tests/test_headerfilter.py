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

SPECS = {
    0: ColumnSpec("text"), 2: ColumnSpec("range", "s", decimals=2, maximum=99_999), 3: ColumnSpec("values"),
    4: ColumnSpec("range", "BPM", maximum=999), 5: ColumnSpec("values"), 6: ColumnSpec("text"),
    7: ColumnSpec("range", maximum=9_999), SampleTreeModel.COL_SIMILARITY: ColumnSpec("range", "%", maximum=100, scale=100.0),
}


def _view(app):
    model = SampleTreeModel(lambda _id: None)
    model.set_rows(ROWS)
    proxy = ListProxy()
    proxy.setSourceModel(model)
    view = QTreeView()
    view.setModel(proxy)
    view.setSortingEnabled(False)
    header = FilterHeader(SPECS, view)
    header.install_on(view)
    header.filter_changed.connect(proxy.set_column_filter)
    header.sort_requested.connect(view.sortByColumn)
    view.resize(900, 300)
    view.show()
    QApplication.processEvents()                  # lay the header out before anything clicks it
    return model, proxy, view, header


def _names(proxy) -> list[str]:
    return [proxy.data(proxy.index(r, 0)) for r in range(proxy.rowCount())]


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
        assert model.distinct_values(3) == ["loop", "multi-hit", "one-shot"]
        assert model.distinct_values(5) == ["", "Am", "C"]
        proxy.set_column_filter(6, ColumnFilter(text="percussion"))
        assert _names(proxy) == ["kick.wav", "loop.wav"] and proxy.filtered_columns == {6}
        proxy.set_column_filter(7, ColumnFilter(low=1))
        assert _names(proxy) == ["loop.wav"]
        proxy.set_column_filter(6, None)
        proxy.set_column_filter(7, ColumnFilter())                       # inactive: clears
        assert _names(proxy) == ["kick.wav", "loop.wav", "pad.wav"] and not proxy.filtered_columns
        proxy.set_column_filter(4, ColumnFilter(low=100.0))              # no tempo: never in a range
        assert _names(proxy) == ["loop.wav"]
    finally:
        view.close()


def test_popups_edit_each_kind_and_the_header_opens_them(app):
    model, proxy, view, header = _view(app)
    try:
        view.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        assert header.sortIndicatorSection() == 2 and _names(proxy)[0] == "pad.wav"

        # a click on a section opens its popup and leaves the sort where it was
        x = header.sectionViewportPosition(3) + 10
        QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x, 8))
        app.processEvents()
        popup = header.popup
        assert popup is not None and popup._list is not None and popup._list.count() == 3
        assert (header.sortIndicatorSection(), header.sortIndicatorOrder()) == (2, Qt.SortOrder.DescendingOrder)
        popup._list.item(0).setCheckState(Qt.CheckState.Unchecked)         # "loop" off
        assert _names(proxy) == ["pad.wav", "kick.wav"] and 3 in header.filters
        assert not view.grab().isNull()                                     # the section paints its dot
        popup._check_all(True)
        assert 3 not in header.filters and len(_names(proxy)) == 3
        popup.close()

        # the popup's buttons sort
        header.open_filter(0)
        header.popup._sort(Qt.SortOrder.AscendingOrder)
        assert _names(proxy) == ["kick.wav", "loop.wav", "pad.wav"] and header.sortIndicatorSection() == 0

        # text and range editors
        text = FilterPopup(0, "File", SPECS[0], None)
        text._edit.setText("oop")
        assert text.current_filter() == ColumnFilter(text="oop")
        text._edit.clear()
        assert text.current_filter() is None
        length = FilterPopup(2, "Length", SPECS[2], ColumnFilter(low=1.0))
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
