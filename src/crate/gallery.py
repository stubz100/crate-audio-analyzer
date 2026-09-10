"""The design system's gallery — every token and control state on one page.

2026-09-10, on the user's steer (spec §9.1). `.docs/design_studies.md` argued
for this as the keystone of the system rather than a nicety: the look can be
iterated in seconds without launching against a 110k-file index, it grabs
offscreen so a change is reviewable as an image, and a test builds it so a
token that breaks a control is visible rather than discovered in the app.

    uv run crate --gallery

Hover and focus states are not shown — Qt only paints those under a real
pointer — so `:hover` and `:focus` rules stay the style sheet's business.
Everything a static widget *can* show is here.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .design import TOKENS, font, mix
from .icons import SHAPES, pixmap
from .widgets import (
    Chip,
    HelpText,
    MeterList,
    SectionHeader,
    SegmentedControl,
    StatusPill,
    Toolbar,
    make_button,
)

SWATCH = QSize(104, 46)


def _section(title: str) -> SectionHeader:
    """A heading — the component, eating its own cooking."""
    return SectionHeader(title)


def _swatch(colour: QColor, name: str, note: str = "") -> QWidget:
    """A colour chip with its role, its hex and (optionally) what it means."""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(TOKENS.metric.xs)

    chip = QLabel()
    chip.setFixedSize(SWATCH)
    pixmap = QPixmap(SWATCH)
    pixmap.fill(colour)
    painter = QPainter(pixmap)
    painter.setPen(TOKENS.surface.divider)
    painter.drawRect(0, 0, SWATCH.width() - 1, SWATCH.height() - 1)
    painter.end()
    chip.setPixmap(pixmap)
    layout.addWidget(chip)

    caption = QLabel(f"{name}\n{colour.name()}" + (f"\n{note}" if note else ""))
    caption.setFont(font("micro"))
    caption.setStyleSheet(f"color: {TOKENS.ink.muted.name()};")
    layout.addWidget(caption)
    layout.addStretch(1)
    return box


def _row(*widgets: QWidget, spacing: int | None = None) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(TOKENS.metric.md if spacing is None else spacing)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch(1)
    return holder


def _ramp(low: QColor, high: QColor, steps: int = 12) -> QWidget:
    """The score gradient as the map actually blends it."""
    strip = QLabel()
    width, height = steps * 28, 26
    pixmap = QPixmap(width, height)
    painter = QPainter(pixmap)
    for i in range(steps):
        painter.fillRect(i * 28, 0, 28, height, mix(low, high, i / (steps - 1)))
    painter.end()
    strip.setPixmap(pixmap)
    return strip


class GalleryWindow(QMainWindow):
    """Every token and control state the system defines, on one scrollable page."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Crate — design system")
        self.resize(1080, 900)

        page = QWidget()
        column = QVBoxLayout(page)
        m = TOKENS.metric
        column.setContentsMargins(m.xl, m.lg, m.xl, m.xl)
        column.setSpacing(m.lg)

        for build in (
            self._surfaces, self._ink, self._state, self._data,
            self._typography, self._scale, self._icons, self._components,
            self._controls, self._list,
        ):
            column.addWidget(build())
        column.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.setCentralWidget(scroll)

    # --- tokens ---

    def _surfaces(self) -> QWidget:
        box = QGroupBox("Surfaces — elevation by value, not by outline")
        layout = QVBoxLayout(box)
        s = TOKENS.surface
        layout.addWidget(
            _row(
                _swatch(s.canvas, "canvas", "painted views"),
                _swatch(s.sunken, "sunken", "lists, inputs"),
                _swatch(s.ground, "ground", "the window"),
                _swatch(s.panel, "panel", "grouped"),
                _swatch(s.raised, "raised", "controls"),
                _swatch(s.overlay, "overlay", "menus"),
                _swatch(s.hairline, "hairline", "barely seen"),
                _swatch(s.divider, "divider", "meant to be seen"),
            )
        )
        return box

    def _ink(self) -> QWidget:
        box = QGroupBox("Ink")
        layout = QVBoxLayout(box)
        for name, colour, use in (
            ("primary", TOKENS.ink.primary, "values, file names, the thing itself"),
            ("secondary", TOKENS.ink.secondary, "labels, control text"),
            ("muted", TOKENS.ink.muted, "micro-labels, headers, the status bar"),
            ("bright", TOKENS.ink.bright, "the selected row"),
        ):
            line = QLabel(f"{name} — {use}")
            line.setStyleSheet(f"color: {colour.name()};")
            layout.addWidget(line)
        return box

    def _state(self) -> QWidget:
        box = QGroupBox("State — never decoration")
        layout = QVBoxLayout(box)
        st = TOKENS.state
        layout.addWidget(
            _row(
                _swatch(st.accent, "accent", "selection, focus"),
                _swatch(st.accent_dim, "accent_dim", "pressed, progress"),
                _swatch(st.accent_fill, "accent_fill", "selected row"),
                _swatch(st.danger, "danger", "destructive"),
                _swatch(st.positive, "positive", "confirmed"),
            )
        )
        return box

    def _data(self) -> QWidget:
        box = QGroupBox("Data — what the audio is, held apart from the chrome")
        layout = QVBoxLayout(box)
        d = TOKENS.data
        layout.addWidget(
            _row(
                _swatch(d.wave, "wave"),
                _swatch(d.envelope, "envelope", "attack/decay"),
                _swatch(d.segment, "segment", "automatic"),
                _swatch(d.segment_manual, "segment_manual"),
                _swatch(d.unscored, "unscored", "no score yet"),
            )
        )
        layout.addWidget(_section("Score gradient — score_low → score_high"))
        layout.addWidget(_ramp(d.score_low, d.score_high))
        return box

    def _typography(self) -> QWidget:
        box = QGroupBox("Type")
        layout = QVBoxLayout(box)
        ty = TOKENS.type
        for role, note in (
            ("body", f"{ty.body}pt {ty.ui} — values and controls"),
            ("label", f"{ty.label}pt — tabs, captions"),
            ("micro", f"{ty.micro}pt uppercase, {ty.tracking} tracking — headers, group titles"),
        ):
            line = QLabel(f"{role} · {note}")
            line.setFont(font(role))
            layout.addWidget(line)

        layout.addWidget(_section("Numerals — the list's columns align by alignment, not by face"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(TOKENS.metric.xl)
        for col, (title, mono) in enumerate((("Segoe UI (tabular)", False), (ty.mono, True))):
            head = QLabel(title)
            head.setFont(font("micro"))
            head.setStyleSheet(f"color: {TOKENS.ink.muted.name()};")
            grid.addWidget(head, 0, col)
            for row, value in enumerate(("0.30 s", "9.60 s", "12.80 s", "111.11"), start=1):
                cell = QLabel(value)
                cell.setFont(font("body", mono=mono))
                cell.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                grid.addWidget(cell, row, col)
        holder = QWidget()
        holder.setLayout(grid)
        layout.addWidget(holder)
        return box

    def _scale(self) -> QWidget:
        box = QGroupBox("Spacing and radius")
        layout = QVBoxLayout(box)
        m = TOKENS.metric
        bars = QWidget()
        row = QHBoxLayout(bars)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(m.lg)
        for name, value in (("xs", m.xs), ("sm", m.sm), ("md", m.md), ("lg", m.lg), ("xl", m.xl)):
            cell = QLabel(f"{name}\n{value}px")
            cell.setFont(font("micro"))
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.setFixedWidth(max(value, 28))
            cell.setStyleSheet(
                f"background: {TOKENS.state.accent_fill.name()};"
                f"color: {TOKENS.ink.secondary.name()};"
                f"border-radius: {m.radius_sm}px; padding: {m.sm}px 0;"
            )
            row.addWidget(cell)
        row.addStretch(1)
        layout.addWidget(bars)
        layout.addWidget(
            _section(f"radius_sm {m.radius_sm}px · radius_md {m.radius_md}px — two, not four")
        )
        return box


    # --- components (layer 2) ---

    def _icons(self) -> QWidget:
        box = QGroupBox("Icons — drawn paths, tinted per state")
        layout = QVBoxLayout(box)
        strip = QWidget()
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TOKENS.metric.lg)
        for name in SHAPES:
            cell = QVBoxLayout()
            cell.setSpacing(TOKENS.metric.xs)
            glyph = QLabel()
            glyph.setPixmap(pixmap(name, TOKENS.ink.secondary, 20))
            glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption = QLabel(name)
            caption.setFont(font("micro"))
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption.setStyleSheet(f"color: {TOKENS.ink.muted.name()};")
            cell.addWidget(glyph)
            cell.addWidget(caption)
            holder = QWidget()
            holder.setLayout(cell)
            row.addWidget(holder)
        row.addStretch(1)
        layout.addWidget(strip)
        layout.addWidget(_section("The same icon at four token colours"))
        tints = QWidget()
        tint_row = QHBoxLayout(tints)
        tint_row.setContentsMargins(0, 0, 0, 0)
        tint_row.setSpacing(TOKENS.metric.lg)
        for name, colour in (
            ("muted", TOKENS.ink.muted),
            ("secondary", TOKENS.ink.secondary),
            ("accent", TOKENS.state.accent),
            ("danger", TOKENS.state.danger),
        ):
            cell = QLabel()
            cell.setPixmap(pixmap("anchor", colour, 22))
            cell.setToolTip(name)
            tint_row.addWidget(cell)
        tint_row.addStretch(1)
        layout.addWidget(tints)
        return box

    def _components(self) -> QWidget:
        box = QGroupBox("Components — layer 2")
        layout = QVBoxLayout(box)

        layout.addWidget(_section("Segmented control — one control, not two buttons"))
        switch = SegmentedControl([("list", "List"), ("map", "Map")])
        wide = SegmentedControl(
            [("new", "New/changed"), ("all", "Everything"), ("anchor", "Anchor only")]
        )
        wide.set_current("all")
        layout.addWidget(_row(switch, wide))

        layout.addWidget(_section("Toolbar — the row decides the widths, groups are divided"))
        bar = Toolbar()
        bar.add_group(
            make_button(icon_name="play", intent="primary", tooltip="Play"),
            make_button(icon_name="stop", tooltip="Stop"),
            equal_width=False,
        )
        bar.add_group(make_button("Spectrum", icon_name="spectrum", checkable=True))
        # A label naming the group, then icon-only actions joined to it without a
        # rule — the transport row's shape since 2026-09-10 (the user's steer:
        # "Save segment" / "Discard" / "Delete segment" were needlessly long).
        segment_label = QLabel("Segment")
        segment_label.setObjectName("sectionHeader")
        bar.add_group(segment_label, equal_width=False)
        bar.add_group(
            make_button(icon_name="save", tooltip="Save"),
            make_button(icon_name="close", intent="quiet", tooltip="Discard"),
            make_button(icon_name="trash", intent="danger", tooltip="Delete"),
            equal_width=False,
            divided=False,
        )
        bar.add_stretch()
        bar.add_widget(StatusPill("2 unsaved", "warn"))
        layout.addWidget(bar)

        layout.addWidget(_section("Meters — the tag scores and the axis differences, one object"))
        meters = MeterList()
        meters.set_rows(
            [("snare drum", 0.37), ("hand clap", 0.31), ("percussion", 0.29), ("tom drum", 0.26)],
            clickable=True,
        )
        differences = MeterList()
        differences.set_rows(
            [("Amplitude", 0.62), ("Pitch", None), ("Timbre", 0.18)],
            tone=TOKENS.data.segment,
        )
        pair = QWidget()
        pair_row = QHBoxLayout(pair)
        pair_row.setContentsMargins(0, 0, 0, 0)
        pair_row.setSpacing(TOKENS.metric.xl)
        pair_row.addWidget(meters, stretch=1)
        pair_row.addWidget(differences, stretch=1)
        layout.addWidget(pair)

        layout.addWidget(_section("Chips, pills and folded prose"))
        layout.addWidget(
            _row(
                Chip("kick drum"), Chip("percussion"), Chip("one-shot"),
                StatusPill("indexed", "neutral"),
                StatusPill("anchored", "active"),
                StatusPill("needs review", "warn"),
                StatusPill("stale", "danger"),
            )
        )
        help_ = HelpText(
            "Tick a folder to put it in scope: the list, the map, Rescan and Recompute cover "
            "the ticked folders. Unticked folders stay in the index, dormant."
        )
        help_.set_open(True)
        layout.addWidget(help_)
        return box

    # --- controls ---

    def _controls(self) -> QWidget:
        box = QGroupBox("Styled Qt primitives — what the style sheet does to stock widgets")
        layout = QVBoxLayout(box)

        layout.addWidget(_section("Buttons — intent, not just state"))
        default = QPushButton("Save segment")
        checked = QPushButton("Spectrum")
        checked.setCheckable(True)
        checked.setChecked(True)
        disabled = QPushButton("Discard")
        disabled.setEnabled(False)
        flat = QPushButton("kick drum")
        flat.setFlat(True)
        danger = QPushButton("Delete segment")
        danger.setObjectName("danger")
        play = QPushButton("▶")
        play.setObjectName("play")
        anchor = QPushButton("⚓ Anchor")
        anchor.setObjectName("anchor")
        layout.addWidget(_row(default, checked, disabled, flat, danger, play, anchor))

        layout.addWidget(_section("Inputs"))
        line = QLineEdit("kick drum")
        line.setFixedWidth(160)
        empty = QLineEdit()
        empty.setPlaceholderText("Search…")
        empty.setFixedWidth(160)
        combo = QComboBox()
        combo.addItems(["Transient to transient", "Transient to fixed length"])
        layout.addWidget(_row(line, empty, combo))

        layout.addWidget(_section("Toggles and meters"))
        check = QCheckBox("Auto-play on select")
        check.setChecked(True)
        radio = QRadioButton("Anchored sample only")
        radio.setChecked(True)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setValue(64)
        slider.setFixedWidth(140)
        progress = QProgressBar()
        progress.setValue(62)
        progress.setFixedWidth(180)
        layout.addWidget(_row(check, radio, slider, progress))
        return box

    def _list(self) -> QWidget:
        box = QGroupBox("The list — numeric columns right-aligned")
        layout = QVBoxLayout(box)
        tree = QTreeWidget()
        tree.setColumnCount(5)
        tree.setHeaderLabels(["Folder", "File", "Length", "Type", "BPM"])
        tree.setRootIsDecorated(True)
        tree.setAlternatingRowColors(True)
        tree.setFixedHeight(196)
        rows = [
            ("Ceramic", "Ceramic_Hit01.wav", "0.30 s", "one-shot", ""),
            ("Ceramic", "Ceramic_Mixed04.wav", "0.63 s", "one-shot", ""),
            ("Foley Only Loops", "AM_SHIFT2_100BPM.wav", "9.60 s", "loop", "100"),
            ("Foley Only Loops", "AM_SHIFT2_128BPM.wav", "7.50 s", "loop", "128"),
            ("Foley Only Loops", "AM_SHIFT2_150BPM_2.wav", "12.80 s", "loop", "150"),
        ]
        for folder, name, length, kind, bpm in rows:
            item = QTreeWidgetItem([folder, name, length, kind, bpm])
            for column in (2, 4):
                item.setTextAlignment(
                    column, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            tree.addTopLevelItem(item)
        hit = QTreeWidgetItem(["", "↳ hit @ 2.10 s", "0.42 s", "segment", ""])
        hit.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        tree.topLevelItem(2).addChild(hit)
        tree.expandAll()
        tree.setCurrentItem(tree.topLevelItem(0))
        header = tree.header()
        for column in range(5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(tree)
        return box


def show_gallery() -> GalleryWindow:
    """Build and show the gallery — the `--gallery` entry point."""
    window = GalleryWindow()
    window.show()
    return window
