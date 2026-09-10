"""Design direction studies (spec §9.1): three token sets on the real window.

The look landed 2026-09-07 as one dark theme (`theme.py` — Fusion, a palette,
a style sheet, the same constants used by the painted views). 2026-09-10, the
user: the beta needs a much sleeker UI, and a design system of its own. This
script is the first step of that — it renders the *actual* window against a
real index under a candidate token set, so a direction is chosen by looking
rather than by describing.

A **token set** (`Tokens`) is the layer `theme.py` never had: a value ramp
wide enough that elevation comes from surface value instead of a 1px outline
on everything, semantic text/state roles, one spacing and radius scale, a
type scale, and switches for the choices that separate the directions
(uppercase micro-labels, alternating rows, bevelled buttons). `build_qss`
and `build_palette` turn one into Qt's two styling channels; `patch_theme`
rebinds the constants the painted views (map, waveform, tag bars, vector
strip, the list delegates) import, so a study covers the painted surfaces
too and not just the widgets.

    uv run python scripts/design_studies.py --variant all

Writes `<variant>_window.png` / `<variant>_recompute.png` per direction plus
three `compare_*.png` sheets that stack the same crop from each, into
`.docs/design/`. Findings are written up in `.docs/design_studies.md`.

Two mechanics worth knowing, both established here (2026-09-10):

* **A variant needs its own process.** The painted views do
  `from .theme import ACCENT`, which binds the value at import; mutating
  `crate.theme` afterwards would not reach them. So the constants are
  patched *before* `crate.main` is first imported, and `--variant all`
  re-runs this script once per direction.
* **The offscreen platform has an empty font database on this machine** —
  `QFontDatabase.families()` returns `[]`, so every glyph rasterises as a
  tofu box while metrics stay correct. `QFontDatabase.addApplicationFont`
  on the files in `C:\\Windows\\Fonts` fixes it. Any offscreen grab that is
  meant to be *looked at* (rather than measured) needs `load_fonts()` first.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QRect, QSettings, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QFontDatabase,
    QImage,
    QPainter,
    QPalette,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

#: Faces loaded by hand for the offscreen grabs (see the module docstring).
FONT_FILES = (
    "segoeui", "segoeuib", "segoeuisl", "segoeuil",
    "consola", "consolab", "CascadiaMono",
)

WINDOW_SIZE = (1600, 950)


# --- the token layer -------------------------------------------------------


@dataclass(frozen=True)
class Tokens:
    """One design direction.

    Surfaces run darkest to lightest: `canvas` is what the painted views draw
    on, `sunken` the wells (lists, inputs), `bg` the window ground, `panel` a
    grouped surface, `raised` a control, `overlay` a menu or popup. `line` is
    a hairline that should barely register; `line_strong` a divider meant to
    be seen.
    """

    name: str
    canvas: str
    sunken: str
    bg: str
    panel: str
    raised: str
    overlay: str
    line: str
    line_strong: str
    text: str
    text_dim: str
    text_mute: str
    text_bright: str
    accent: str
    accent_dim: str
    accent_fill: str
    danger: str
    positive: str
    segment: str
    envelope: str
    score_low: str
    score_high: str
    radius_sm: int
    radius_md: int
    pad_v: int
    pad_h: int
    row_pad: int
    ui: str
    mono: str
    size_body: str
    size_label: str
    size_micro: str
    boxed_groups: bool
    alt_rows: bool
    upper_labels: bool
    tracking: str
    bevel: bool


#: A — the idiom of a production tool. Near-black ground, hairlines instead of
#: boxes, uppercase micro-labels, one restrained accent reserved for state, no
#: alternating rows. The waveform, spectrogram and map become the only
#: saturated things on screen, and the list keeps its density.
INSTRUMENT = Tokens(
    name="instrument",
    canvas="#0a0b0d", sunken="#0d0e11", bg="#101216", panel="#161920",
    raised="#1e222a", overlay="#242832", line="#20242c", line_strong="#333944",
    text="#e6e8ec", text_dim="#949aa6", text_mute="#5f6673", text_bright="#ffffff",
    accent="#6fb3d9", accent_dim="#2b4a5e", accent_fill="#1b3543",
    danger="#e0685f", positive="#5fb87f", segment="#d9a441", envelope="#b4658f",
    score_low="#243244", score_high="#f2c14e",
    radius_sm=2, radius_md=3, pad_v=4, pad_h=10, row_pad=3,
    ui="Segoe UI", mono="Cascadia Mono",
    size_body="9pt", size_label="8pt", size_micro="7pt",
    boxed_groups=True, alt_rows=False, upper_labels=True, tracking="1.2px", bevel=False,
)

#: B — a modern desktop app rather than a DAW. Wider contrast, generous but
#: strictly consistent spacing, sentence-case headings, real card elevation.
#: Costs roughly a third of the list's rows per screen.
CALM = Tokens(
    name="calm",
    canvas="#12141a", sunken="#181b22", bg="#1a1d25", panel="#22262f",
    raised="#2b303b", overlay="#333945", line="#2f3540", line_strong="#3f4653",
    text="#f0f1f5", text_dim="#a9afbd", text_mute="#727a8a", text_bright="#ffffff",
    accent="#7d8cf5", accent_dim="#3d4694", accent_fill="#2c3260",
    danger="#f0736b", positive="#63c88c", segment="#e8b35c", envelope="#d081ab",
    score_low="#2b3350", score_high="#ffd479",
    radius_sm=5, radius_md=8, pad_v=7, pad_h=14, row_pad=6,
    ui="Segoe UI", mono="Consolas",
    size_body="10pt", size_label="9pt", size_micro="8pt",
    boxed_groups=True, alt_rows=True, upper_labels=False, tracking="0px", bevel=False,
)

#: C — warm ground, bevelled controls, amber readouts. The most characterful
#: and the highest risk: the chrome competes with the audio content for
#: attention, which is the opposite of what a browser over 110k files wants.
HARDWARE = Tokens(
    name="hardware",
    canvas="#0f0d0a", sunken="#141210", bg="#1a1715", panel="#221e19",
    raised="#2c2720", overlay="#332d25", line="#332d24", line_strong="#4a4133",
    text="#ece7dc", text_dim="#a89e8b", text_mute="#6f6656", text_bright="#fff8e8",
    accent="#e8a33d", accent_dim="#6b4a18", accent_fill="#40300f",
    danger="#d9573f", positive="#7fbf6a", segment="#e8c25c", envelope="#c96f5a",
    score_low="#3a2f1c", score_high="#ffd97a",
    radius_sm=3, radius_md=4, pad_v=5, pad_h=12, row_pad=4,
    ui="Segoe UI", mono="Cascadia Mono",
    size_body="9pt", size_label="8pt", size_micro="7pt",
    boxed_groups=True, alt_rows=False, upper_labels=True, tracking="1.4px", bevel=True,
)

SETS = {t.name: t for t in (INSTRUMENT, CALM, HARDWARE)}
VARIANTS = ("baseline", *SETS)

#: `theme.py` derives from `design.py` since 2026-09-10, so "baseline" now
#: renders whatever the shipped direction is — not the pre-token theme the
#: committed comparison sheets record. The sheets are the historical record.
RENDERABLE = (*VARIANTS, "gallery")


# --- Qt's two styling channels ---------------------------------------------


def _lighten(hexstr: str, amount: int) -> str:
    c = QColor(hexstr)
    return QColor(
        min(c.red() + amount, 255),
        min(c.green() + amount, 255),
        min(c.blue() + amount, 255),
    ).name()


def build_qss(t: Tokens) -> str:
    """The style sheet for a token set.

    Both `text-transform` and `letter-spacing` were checked against Qt before
    being relied on here (2026-09-10) — Qt honours them, as it does
    `font-variant: small-caps`. What it has no equivalent for is `box-shadow`,
    which is why elevation is a step on the surface ramp rather than a
    shadow, and transitions, which is why nothing here animates.
    """
    up = "text-transform: uppercase;" if t.upper_labels else ""
    track = f"letter-spacing: {t.tracking};" if t.upper_labels else ""
    group_border = f"1px solid {t.line}" if t.boxed_groups else "none"
    btn_bg = (
        f"qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {t.raised},stop:1 {t.panel})"
        if t.bevel
        else t.raised
    )
    alt = _lighten(t.sunken, 6) if t.alt_rows else t.sunken
    return f"""
QWidget {{ color: {t.text}; font-family: "{t.ui}"; font-size: {t.size_body}; }}
QMainWindow, QDialog {{ background: {t.bg}; }}
QToolTip {{ background: {t.overlay}; color: {t.text}; border: 1px solid {t.line_strong};
            padding: 5px 7px; border-radius: {t.radius_sm}px; }}

QGroupBox {{ background: {t.panel}; border: {group_border}; border-radius: {t.radius_md}px;
             margin-top: 15px; padding: {t.pad_v + 4}px {t.pad_h}px {t.pad_v + 2}px {t.pad_h}px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 2px;
                    padding: 0 3px 0 0; color: {t.text_mute};
                    font-size: {t.size_micro}; font-weight: 700; {up} {track} }}

QPushButton {{ background: {btn_bg}; border: 1px solid {t.line_strong};
               border-radius: {t.radius_sm}px; padding: {t.pad_v}px {t.pad_h}px;
               color: {t.text_dim}; min-height: 16px; }}
QPushButton:hover {{ background: {t.overlay}; color: {t.text}; border-color: {t.accent_dim}; }}
QPushButton:pressed {{ background: {t.accent_fill}; }}
QPushButton:checked {{ background: {t.accent_fill}; border-color: {t.accent}; color: {t.text_bright}; }}
QPushButton:disabled {{ color: {t.text_mute}; background: transparent; border-color: {t.line}; }}
QPushButton:flat {{ background: transparent; border: 1px solid {t.line_strong};
                    border-radius: {t.radius_md}px; padding: 2px 8px; }}
QPushButton#play {{ background: {t.accent_fill}; border-color: {t.accent}; color: {t.accent};
                    font-size: 15pt; padding: 0px 12px; }}
QPushButton#play:hover {{ background: {t.accent_dim}; color: {t.text_bright}; }}
QPushButton#anchor {{ border-color: {t.segment}; color: {t.segment}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: {t.sunken}; border: 1px solid {t.line}; border-radius: {t.radius_sm}px;
    padding: {t.pad_v - 1}px {t.pad_h - 4}px; color: {t.text};
    selection-background-color: {t.accent_dim}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {t.accent}; }}
QComboBox::drop-down {{ border: none; width: 16px; }}
QComboBox QAbstractItemView {{ background: {t.overlay}; border: 1px solid {t.line_strong};
                               selection-background-color: {t.accent_fill}; }}

QTreeView, QTableView, QListWidget {{
    background: {t.sunken}; alternate-background-color: {alt};
    border: none; color: {t.text};
    selection-background-color: {t.accent_fill}; selection-color: {t.text_bright};
    gridline-color: {t.line}; outline: none; }}
QTreeView::item, QTableView::item {{ padding: {t.row_pad}px 4px; border: none; }}
QTreeView::item:hover, QTableView::item:hover {{ background: {t.raised}; }}
QTreeView::item:selected, QTableView::item:selected {{ background: {t.accent_fill}; color: {t.text_bright}; }}

QHeaderView {{ background: {t.bg}; }}
QHeaderView::section {{ background: {t.bg}; color: {t.text_mute};
    border: none; border-bottom: 1px solid {t.line_strong};
    padding: {t.pad_v + 1}px 6px; font-size: {t.size_micro}; font-weight: 700; {up} {track} }}
QHeaderView::section:hover {{ color: {t.text_dim}; }}

QTabWidget::pane {{ border: none; border-top: 1px solid {t.line}; top: -1px; background: {t.bg}; }}
QTabBar::tab {{ background: transparent; color: {t.text_mute}; border: none;
    border-bottom: 2px solid transparent; padding: {t.pad_v + 2}px {t.pad_h + 2}px;
    margin-right: 2px; font-size: {t.size_label}; {up} {track} }}
QTabBar::tab:selected {{ color: {t.text}; border-bottom: 2px solid {t.accent}; }}
QTabBar::tab:hover {{ color: {t.text_dim}; }}

QProgressBar {{ background: {t.sunken}; border: 1px solid {t.line}; border-radius: {t.radius_sm}px;
    text-align: center; color: {t.text_dim}; min-height: 14px; font-size: {t.size_micro}; }}
QProgressBar::chunk {{ background: {t.accent_dim}; border-radius: {t.radius_sm}px; }}

QSlider::groove:horizontal {{ height: 3px; background: {t.line_strong}; border-radius: 1px; }}
QSlider::sub-page:horizontal {{ background: {t.accent}; border-radius: 1px; }}
QSlider::handle:horizontal {{ background: {t.text}; border: none;
    width: 9px; height: 9px; margin: -4px 0; border-radius: 4px; }}

QCheckBox, QRadioButton {{ spacing: 6px; color: {t.text_dim}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 12px; height: 12px;
    border: 1px solid {t.line_strong}; border-radius: {t.radius_sm}px; background: {t.sunken}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t.line_strong}; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {t.text_mute}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {t.line_strong}; border-radius: 4px; min-width: 24px; }}

QSplitter::handle {{ background: {t.bg}; }}
QSplitter::handle:horizontal {{ width: 5px; }}
QSplitter::handle:vertical {{ height: 5px; }}

QStatusBar {{ background: {t.bg}; color: {t.text_mute}; border-top: 1px solid {t.line};
              font-size: {t.size_micro}; }}
QStatusBar::item {{ border: none; }}

QMenu {{ background: {t.overlay}; color: {t.text}; border: 1px solid {t.line_strong};
         padding: 4px 0; border-radius: {t.radius_md}px; }}
QMenu::item {{ padding: 5px 20px 5px 22px; }}
QMenu::item:selected {{ background: {t.accent_fill}; }}
QMenu::separator {{ height: 1px; background: {t.line}; margin: 3px 6px; }}

QFrame#filterPopup {{ background: {t.overlay}; border: 1px solid {t.line_strong};
                      border-radius: {t.radius_md}px; }}
QLabel#caption {{ color: {t.text_mute}; font-size: {t.size_label}; }}
QLabel#nowPlaying {{ color: {t.text}; font-weight: 600; }}
QWidget#header {{ background: {t.bg}; border-bottom: 1px solid {t.line}; }}
"""


def build_palette(t: Tokens) -> QPalette:
    """The palette for a token set — what Fusion draws that QSS never reaches."""
    p, r = QPalette(), QPalette.ColorRole
    for role, value in (
        (r.Window, t.bg), (r.WindowText, t.text), (r.Base, t.sunken),
        (r.AlternateBase, t.panel), (r.ToolTipBase, t.overlay), (r.ToolTipText, t.text),
        (r.Text, t.text), (r.PlaceholderText, t.text_mute), (r.Button, t.raised),
        (r.ButtonText, t.text_dim), (r.BrightText, t.text_bright),
        (r.Highlight, t.accent_fill), (r.HighlightedText, t.text_bright),
        (r.Link, t.accent), (r.Mid, t.line_strong), (r.Dark, t.canvas), (r.Light, t.raised),
    ):
        p.setColor(role, QColor(value))
    disabled = QPalette.ColorGroup.Disabled
    for role in (r.Text, r.ButtonText, r.WindowText):
        p.setColor(disabled, role, QColor(t.text_mute))
    return p


def patch_theme(t: Tokens) -> None:
    """Rebind what the painted views import — call before `crate.main` loads.

    `theme.BG` is the *canvas* here, not the window ground: the map and the
    waveform draw on the darkest surface, while the widgets around them sit
    on `bg`. That split is one of the things a real token layer would make
    explicit and `theme.py` currently cannot express.
    """
    import crate.theme as th

    for name, value in (
        ("BG", t.canvas), ("PANEL", t.panel), ("FIELD", t.sunken), ("RAISED", t.raised),
        ("BORDER", t.line_strong), ("TEXT", t.text), ("TEXT_DIM", t.text_dim),
        ("ACCENT", t.accent), ("ACCENT_DIM", t.accent_dim), ("AMBER", t.segment),
        ("GREEN", t.positive), ("PINK", t.envelope), ("RED", t.danger),
        ("WHITE", t.text_bright), ("SCORE_LOW", t.score_low), ("SCORE_HIGH", t.score_high),
    ):
        setattr(th, name, QColor(value))


_APP: QApplication | None = None


def application() -> QApplication:
    """The one QApplication, kept referenced — Qt crashes if it is collected."""
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def load_fonts() -> int:
    """Load the faces by file — the offscreen platform ships no font database."""
    loaded = 0
    for stem in FONT_FILES:
        if QFontDatabase.addApplicationFont(f"C:\\Windows\\Fonts\\{stem}.ttf") >= 0:
            loaded += 1
    return loaded


# --- rendering -------------------------------------------------------------


def render_gallery(out_dir: Path) -> None:
    """Grab the design system's own page — the reference image for the tokens."""
    app = application()
    load_fonts()
    from crate.gallery import GalleryWindow
    from crate.theme import apply_theme

    apply_theme(app)
    window = GalleryWindow()
    window.resize(1080, 1560)
    window.show()
    for _ in range(60):
        app.processEvents()
    page = window.centralWidget().widget()
    page.grab().save(str(out_dir / "gallery.png"))
    print(f"gallery: rendered {page.width()}x{page.height()}")


def render(variant: str, db: Path, out_dir: Path) -> None:
    """Grab the window (and its Recompute tab) under one direction."""
    app = application()
    if not load_fonts():
        print("warning: no fonts loaded — the grabs will be tofu boxes", file=sys.stderr)

    if variant == "baseline":
        from crate.theme import apply_theme

        apply_theme(app)
    else:
        tokens = SETS[variant]
        patch_theme(tokens)
        app.setStyle("Fusion")
        app.setPalette(build_palette(tokens))
        app.setFont(QFont(tokens.ui, 9 if tokens.size_body == "9pt" else 10))
        app.setStyleSheet(build_qss(tokens))

    from crate.main import MainWindow

    # Settings and render cache are throwaway, and must never be the user's own
    # QSettings: a study would otherwise inherit (and overwrite) the real
    # window's geometry, splitters, columns and open tab.
    work = Path(tempfile.mkdtemp(prefix=f"crate-study-{variant}-"))
    settings = QSettings(str(work / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(db_path=db, cache_dir=work / "cache", settings=settings)
    window.resize(*WINDOW_SIZE)
    window.show()

    def pump(ms: int) -> None:
        end = time.time() + ms / 1000
        while time.time() < end:
            app.processEvents()
            time.sleep(0.008)

    pump(4000)                                   # the list loads on a thread
    table = window._table
    index = table.model().index(0, 0)
    table.setCurrentIndex(index)
    selection = table.selectionModel()
    selection.select(
        index,
        selection.SelectionFlag.ClearAndSelect | selection.SelectionFlag.Rows,
    )
    pump(2500)                                   # the waveform is read on a thread
    window.grab().save(str(out_dir / f"{variant}_window.png"))
    window._tabs.setCurrentIndex(2)
    pump(600)
    window.grab().save(str(out_dir / f"{variant}_recompute.png"))
    print(f"{variant}: rendered {table.model().rowCount()} rows")


#: The crops the comparison sheets stack, against WINDOW_SIZE.
CROPS = {
    "list": (QRect(805, 0, 790, 250), "The list — header, rows, selection"),
    "controls": (QRect(0, 484, 800, 210), "Transport row, tabs, Attributes panel"),
    "header": (QRect(0, 0, 800, 190), "Header — view switch, tag bars, vector strip"),
}
CAPTIONS = {
    "baseline": "NOW  —  current theme.py",
    "instrument": "A  —  INSTRUMENT PANEL",
    "calm": "B  —  CALM EDITORIAL",
    "hardware": "C  —  STUDIO HARDWARE",
}


def montage(out_dir: Path, variants: list[str]) -> None:
    """Stack the same crop from each direction, so they compare directly."""
    application()
    load_fonts()
    label_h, gap = 26, 8
    for key, (rect, title) in CROPS.items():
        width = rect.width()
        height = len(variants) * (rect.height() + label_h + gap) + 34
        sheet = QImage(width, height, QImage.Format.Format_RGB32)
        sheet.fill(QColor("#000000"))
        painter = QPainter(sheet)
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.setPen(QColor("#ffffff"))
        painter.drawText(QRect(0, 6, width, 22), Qt.AlignmentFlag.AlignCenter, title)
        y = 34
        for variant in variants:
            source = QImage(str(out_dir / f"{variant}_window.png"))
            painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            painter.setPen(QColor("#8a8f99"))
            painter.drawText(
                QRect(4, y, width, label_h),
                Qt.AlignmentFlag.AlignVCenter,
                CAPTIONS.get(variant, variant),
            )
            y += label_h
            painter.drawImage(0, y, source.copy(rect))
            painter.setPen(QColor("#3a3a3a"))
            painter.drawRect(0, y, width - 1, rect.height() - 1)
            y += rect.height() + gap
        painter.end()
        sheet.save(str(out_dir / f"compare_{key}.png"))
        print(f"compare_{key}.png")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--variant", default="all", choices=("all", *RENDERABLE),
        help="which direction to render ('all' runs each in its own process)",
    )
    parser.add_argument(
        "--db", type=Path, default=Path(".crate_cache/crate.db"),
        help="the index to render against",
    )
    parser.add_argument("--out", type=Path, default=Path(".docs/design"))
    parser.add_argument(
        "--no-montage", action="store_true", help="skip the comparison sheets",
    )
    args = parser.parse_args()

    if args.variant == "gallery":
        args.out.mkdir(parents=True, exist_ok=True)
        render_gallery(args.out)
        return 0

    if not args.db.exists():
        parser.error(
            f"no index at {args.db} — point --db at one, or scan a folder in first"
        )
    args.out.mkdir(parents=True, exist_ok=True)

    if args.variant != "all":
        render(args.variant, args.db, args.out)
        return 0

    # One process per direction: the painted views bind theme constants at
    # import, so a single process could only ever render the first variant.
    for variant in VARIANTS:
        result = subprocess.run(
            [sys.executable, __file__, "--variant", variant,
             "--db", str(args.db), "--out", str(args.out)],
            check=False,
        )
        if result.returncode:
            print(f"{variant}: failed", file=sys.stderr)
            return result.returncode
    if not args.no_montage:
        montage(args.out, list(VARIANTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
