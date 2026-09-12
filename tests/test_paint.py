"""The design system's painting kit (spec §9.1, layer 3).

Offscreen. Each helper is asserted through what it is *for* — a division that
barely registers, a mark that finds the selected row, a ramp that costs one
table rather than an interpolation per point, a texture that recedes.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from crate import paint
from crate.design import TOKENS


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _canvas(width=40, height=20, ground="#000000"):
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(ground))
    return pixmap


def test_hairline_draws_one_line_in_the_token_colour(app):
    pixmap = _canvas()
    painter = QPainter(pixmap)
    paint.hairline(painter, QPointF(0, 10), QPointF(40, 10))
    painter.end()
    image = pixmap.toImage()
    assert image.pixelColor(20, 10) == TOKENS.surface.hairline
    assert image.pixelColor(20, 4) == QColor("#000000"), "a hairline should touch one row"


def test_selection_edge_marks_only_the_left(app):
    """Direction A's quiet selection fill needed a mark to find the row."""
    pixmap = _canvas()
    painter = QPainter(pixmap)
    paint.selection_edge(painter, QRectF(0, 0, 40, 20))
    painter.end()
    image = pixmap.toImage()
    assert image.pixelColor(0, 10) == TOKENS.state.accent
    assert image.pixelColor(1, 10) == TOKENS.state.accent
    assert image.pixelColor(6, 10) == QColor("#000000"), "the edge is a mark, not a bar"


def test_ramp_runs_low_to_high_and_is_cached(app):
    low, high = TOKENS.data.score_low, TOKENS.data.score_high
    table = paint.ramp(low, high, 256)
    assert len(table) == 256
    assert table[0] == low
    assert table[-1] == high
    assert paint.ramp(low, high, 256) is table, "the table should be cached, not rebuilt"


def test_ramp_climbs(app):
    table = paint.ramp(TOKENS.data.score_low, TOKENS.data.score_high, 16)
    lightness = [c.lightness() for c in table]
    assert lightness == sorted(lightness)


def test_along_clamps_outside_zero_to_one(app):
    table = paint.ramp(TOKENS.data.score_low, TOKENS.data.score_high, 16)
    assert paint.along(table, -5.0) == table[0]
    assert paint.along(table, 0.0) == table[0]
    assert paint.along(table, 1.0) == table[-1]
    assert paint.along(table, 99.0) == table[-1]


def test_damp_compresses_towards_the_ceiling(app):
    """Item 7: the loudest pixels carried the least information."""
    assert paint.damp(0.0) == 0.0
    assert paint.damp(1.0) == pytest.approx(paint.TEXTURE_CEILING)
    assert paint.damp(1.0) < 1.0
    assert paint.damp(5.0) == paint.damp(1.0), "t must clamp"
    assert paint.damp(0.5) < paint.damp(0.9), "order is preserved"


def test_the_strip_is_quieter_than_it_was(app):
    """The regression this exists to prevent: a full-contrast CLAP strip."""
    from crate.theme import AMBER, mix

    loud = mix(TOKENS.surface.sunken, AMBER, 1.0)
    quiet = mix(TOKENS.surface.sunken, AMBER, paint.damp(1.0))
    assert quiet.lightness() < loud.lightness()


def test_plot_inset_holds_a_signal_off_the_edges(app):
    assert paint.PLOT_INSET > 2.0, "2px was the old value that read as clipped"
