"""The design system's icons and components (spec §9.1, layer 2).

Offscreen. These assert the behaviour the panels will lean on — that a
toolbar group really does equalise its widths, that a meter with no value
draws something, that a segmented control is exclusive and signals once —
rather than pixel positions, which are the direction's to change.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from crate import icons
from crate.design import TOKENS
from crate.widgets import (
    Chip,
    HelpText,
    Meter,
    MeterList,
    SectionHeader,
    SegmentedControl,
    StatusPill,
    Toolbar,
    make_button,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# --- icons ---


def test_every_icon_draws_something(app):
    """A named icon that paints an empty pixmap is worse than a missing one."""
    for name in icons.SHAPES:
        pixmap = icons.pixmap(name, QColor("#ffffff"), 24)
        assert not pixmap.isNull(), name
        image = pixmap.toImage()
        painted = sum(
            1
            for x in range(image.width())
            for y in range(image.height())
            if image.pixelColor(x, y).alpha() > 0
        )
        assert painted > 8, f"{name} drew {painted} pixels"


def test_icons_are_tinted_by_the_colour_asked_for(app):
    """The whole point of paths over glyphs: state can colour them."""
    accent = icons.pixmap("play", TOKENS.state.accent, 24).toImage()
    danger = icons.pixmap("play", TOKENS.state.danger, 24).toImage()
    assert accent != danger

    def opaque(image):
        return [
            image.pixelColor(x, y)
            for x in range(image.width())
            for y in range(image.height())
            if image.pixelColor(x, y).alpha() == 255
        ]

    assert opaque(accent), "nothing fully opaque to sample"
    assert all(c == TOKENS.state.accent for c in opaque(accent))


def test_unknown_icon_is_an_error(app):
    with pytest.raises(KeyError):
        icons.pixmap("nonesuch", QColor("#ffffff"))


def test_icon_carries_a_disabled_pixmap(app):
    result = icons.icon("save", TOKENS.ink.secondary, disabled=TOKENS.ink.muted)
    assert not result.isNull()
    assert not result.pixmap(16).isNull()


def test_icons_scale_crisply(app):
    for size in (12, 16, 32):
        assert icons.pixmap("anchor", QColor("#ffffff"), size).size().width() == size


# --- components ---


def test_toolbar_group_equalises_widths(app):
    """Item 5 of the diagnosis: the row decides the widths, not the labels."""
    bar = Toolbar()
    save = make_button("Save")
    discard = make_button("Discard")
    delete = make_button("Delete segment")
    bar.add_group(save, discard, delete)
    bar.show()
    app.processEvents()
    widths = {b.minimumWidth() for b in (save, discard, delete)}
    assert len(widths) == 1, f"widths not equalised: {widths}"
    assert widths.pop() >= delete.sizeHint().width()


def test_toolbar_divides_groups(app):
    bar = Toolbar()
    bar.add_group(make_button("Play"))
    bar.add_group(make_button("Save"), make_button("Discard"))
    assert len(bar.groups) == 2
    assert [len(g) for g in bar.groups] == [1, 2]


def test_toolbar_ignores_an_empty_group(app):
    bar = Toolbar()
    bar.add_group()
    assert bar.groups == []


def test_button_intent_is_a_property_the_stylesheet_can_see(app):
    for intent in ("default", "primary", "quiet", "danger"):
        assert make_button("x", intent=intent).property("intent") == intent
    with pytest.raises(KeyError):
        make_button("x", intent="loud")


def test_danger_is_quiet_at_rest_and_colours_on_hover(app):
    """A destructive action must be identifiable without a toolbar that shouts."""
    from crate.design import stylesheet

    css = stylesheet()
    rest = css.split('QPushButton[intent="danger"]:hover')[0]
    assert 'QPushButton[intent="danger"] {' not in rest, "danger should have no resting rule"
    hover = css.split('QPushButton[intent="danger"]:hover')[1].split("}")[0]
    assert TOKENS.state.danger.name() in hover


def test_segmented_control_is_exclusive_and_signals_once(app):
    control = SegmentedControl([("list", "List"), ("map", "Map")])
    seen: list[str] = []
    control.changed.connect(seen.append)
    assert control.current() == "list"
    control.set_current("map")
    control.set_current("map")                  # no second signal for no change
    assert control.current() == "map"
    assert seen == ["map"]
    with pytest.raises(KeyError):
        control.set_current("grid")


def test_segmented_control_needs_an_option(app):
    with pytest.raises(ValueError):
        SegmentedControl([])


def test_segmented_control_click_selects_the_slot_under_it(app):
    control = SegmentedControl([("a", "A"), ("b", "B"), ("c", "C")])
    control.resize(300, 24)
    assert control._index_at(10) == 0
    assert control._index_at(150) == 1
    assert control._index_at(290) == 2
    assert control._index_at(400) == -1


def test_meter_paints_with_and_without_a_value(app):
    """A meter with no value draws its track and an em dash — not the wide
    empty box with a centred "n/a" the Attributes tab uses today."""
    for value in (0.62, None, 0.0, 1.0, 5.0):
        meter = Meter("Amplitude", value)
        meter.resize(280, 20)
        pixmap = meter.grab()
        assert not pixmap.isNull()
        colours = {
            pixmap.toImage().pixel(x, 10) for x in range(0, 280, 4)
        }
        assert len(colours) > 1, f"meter for {value} painted flat"


def test_meter_list_replaces_its_rows(app):
    meters = MeterList()
    meters.set_rows([("a", 0.1), ("b", 0.2)])
    assert [m.label() for m in meters.meters] == ["a", "b"]
    meters.set_rows([("c", 0.3)])
    assert [m.label() for m in meters.meters] == ["c"]
    assert meters.meters[0].value() == 0.3


def test_meter_list_forwards_a_click(app):
    meters = MeterList()
    meters.set_rows([("snare drum", 0.4)], clickable=True)
    seen: list[str] = []
    meters.clicked.connect(seen.append)
    meters.meters[0].clicked.emit("snare drum")
    assert seen == ["snare drum"]


def test_status_pill_tones(app):
    pill = StatusPill("indexed")
    assert pill.tone() == "neutral"
    pill.set_tone("danger")
    assert pill.tone() == "danger"
    assert TOKENS.state.danger.name() in pill.styleSheet()
    with pytest.raises(KeyError):
        pill.set_tone("puce")


def test_section_header_is_the_uppercase_micro_label(app):
    """`text-transform` never reached QGroupBox::title, so the component
    carries it on a QLabel instead."""
    header = SectionHeader("Library")
    assert header.text() == "Library"
    label = header.findChild(type(header._label))
    assert label.objectName() == "sectionHeader"
    header.setText("Recompute")
    assert header.text() == "Recompute"


def test_help_text_starts_folded(app):
    """Prose belongs a click away, not in the primary surface."""
    help_ = HelpText("Tick a folder to put it in scope.")
    assert not help_.is_open()
    assert not help_._body.isVisible()
    help_.set_open(True)
    assert help_.is_open()


def test_chip_is_clickable(app):
    chip = Chip("kick drum")
    seen: list[bool] = []
    chip.clicked.connect(lambda: seen.append(True))
    chip.click()
    assert seen == [True]
    assert chip.cursor().shape() == Qt.CursorShape.PointingHandCursor
