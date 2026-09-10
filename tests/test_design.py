"""The design system's token layer and its gallery (spec §9.1).

Offscreen, like the rest of the GUI tests. What is asserted here is the
*contract* the rest of the application relies on — that every colour resolves
through `design.py`, that `theme.py` still exports what the painted views
import, and that the gallery builds and paints — not particular hex values,
which are the direction's to change.
"""

from __future__ import annotations

import os
from dataclasses import fields

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from crate import design, theme


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_every_token_group_is_colours():
    """The ramp is real: each surface is a distinct, valid colour."""
    surfaces = design.TOKENS.surface
    values = [getattr(surfaces, f.name) for f in fields(surfaces)]
    assert all(isinstance(c, QColor) and c.isValid() for c in values)
    assert len({c.name() for c in values}) == len(values)


def test_surface_ramp_climbs_from_canvas_to_overlay():
    """Elevation is a step in value — that is what replaces the outlines."""
    s = design.TOKENS.surface
    ladder = [s.canvas, s.sunken, s.ground, s.panel, s.raised, s.overlay]
    lightness = [c.lightness() for c in ladder]
    assert lightness == sorted(lightness)
    assert lightness[-1] - lightness[0] >= 30, "the ramp is too narrow to carry elevation"


def test_canvas_is_not_the_window_ground():
    """The old flat namespace could not express this; the token layer must."""
    assert design.TOKENS.surface.canvas != design.TOKENS.surface.ground
    assert theme.BG == design.TOKENS.surface.canvas


def test_theme_exports_what_the_painted_views_import():
    """`theme.__all__` is a contract — mapview, waveform, tagbars and the
    list delegates import these names directly."""
    required = {
        "ACCENT", "ACCENT_DIM", "AMBER", "BG", "BORDER", "FIELD", "GREEN",
        "PINK", "SCORE_HIGH", "SCORE_LOW", "TEXT", "TEXT_DIM", "WHITE",
        "ElidedLabel", "SqueezableWidget", "apply_theme", "mix",
    }
    assert required <= set(theme.__all__)
    for name in required:
        assert hasattr(theme, name), name


def test_every_theme_colour_comes_from_a_token():
    """No colour is decided in `theme.py` any more."""
    t = design.TOKENS
    known = {
        getattr(group, f.name).name()
        for group in (t.surface, t.ink, t.state, t.data)
        for f in fields(group)
    }
    for name in theme.__all__:
        value = getattr(theme, name)
        if isinstance(value, QColor):
            assert value.name() in known, f"{name} is not a token"


def test_mix_blends_and_clamps():
    a, b = QColor("#000000"), QColor("#ffffff")
    assert design.mix(a, b, 0.0) == a
    assert design.mix(a, b, 1.0) == b
    assert design.mix(a, b, 5.0) == b, "t must clamp"
    assert design.mix(a, b, 0.5).red() == 127


def test_font_roles_descend(app):
    body = design.font("body")
    label = design.font("label")
    micro = design.font("micro")
    assert body.pointSizeF() > label.pointSizeF() > micro.pointSizeF()
    assert design.font("body", mono=True).family() == design.TOKENS.type.mono
    with pytest.raises(KeyError):
        design.font("enormous")


def test_stylesheet_builds_from_tokens(app):
    css = design.stylesheet()
    assert design.TOKENS.surface.ground.name() in css
    assert design.TOKENS.state.accent.name() in css
    for selector in ("QTreeView", "QHeaderView::section", "QPushButton", "QTabBar::tab"):
        assert selector in css


def test_group_title_rule_makes_no_transform_claim(app):
    """`text-transform` reaches a QLabel but not the QGroupBox::title
    subcontrol (checked by rendering, 2026-09-10), so the group-title rule
    must not pretend otherwise — `QLabel#sectionHeader` is the real
    uppercase micro-label."""
    css = design.stylesheet()
    title_rule = css.split("QGroupBox::title")[1].split("}")[0]
    assert "text-transform" not in title_rule
    assert "letter-spacing" in title_rule
    section_rule = css.split("QLabel#sectionHeader")[1].split("}")[0]
    assert "text-transform: uppercase" in section_rule


def test_palette_covers_the_roles_fusion_draws(app):
    p = design.palette()
    role = QPalette.ColorRole
    assert p.color(role.Window) == design.TOKENS.surface.ground
    assert p.color(role.Base) == design.TOKENS.surface.sunken
    assert p.color(role.Highlight) == design.TOKENS.state.accent_fill
    disabled = p.color(QPalette.ColorGroup.Disabled, role.Text)
    assert disabled == design.TOKENS.ink.muted


def test_apply_theme_applies_both_channels_and_repeats_cleanly(app):
    """Fusion itself is not observable afterwards — `setStyleSheet` wraps the
    style in a `QStyleSheetStyle` whose `baseStyle()` PySide6 does not expose
    — so what is asserted is the two channels `apply_theme` actually sets."""
    theme.apply_theme(app)
    first = app.styleSheet()
    assert design.TOKENS.state.accent.name() in first
    assert app.palette().color(QPalette.ColorRole.Window) == design.TOKENS.surface.ground
    assert app.font().pointSizeF() == design.TOKENS.type.body
    theme.apply_theme(app)
    assert app.styleSheet() == first


def test_gallery_builds_and_paints(app):
    """The gallery is the system's regression surface: if a token breaks a
    control, this is where it shows."""
    from crate.gallery import GalleryWindow

    window = GalleryWindow()
    window.resize(1000, 1200)
    window.show()
    app.processEvents()
    page = window.centralWidget().widget()
    assert page.height() > 800, "the gallery should be a full page of sections"
    pixmap = page.grab()
    assert not pixmap.isNull()
    image = pixmap.toImage()
    colours = {image.pixel(x, y) for x in range(0, image.width(), 40)
               for y in range(0, image.height(), 40)}
    assert len(colours) > 10, "the gallery painted a flat rectangle"
    window.close()
