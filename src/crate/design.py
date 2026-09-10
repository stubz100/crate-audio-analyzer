"""The design system's token layer (spec §9.1).

2026-09-10, on the user's steer — the beta needed "much sleeker UI", and a
design system of its own. `.docs/design_studies.md` records the diagnosis and
the three directions rendered against the real window; **direction A,
"instrument panel"**, was chosen on density. This module is that direction as
tokens, and `theme.py` now derives every constant it exports from here.

What `theme.py` had was one flat namespace of sixteen colours. What a token
layer adds:

* **A surface ramp wide enough to carry elevation.** `canvas → sunken →
  ground → panel → raised → overlay` spans about 60 levels of lightness where
  the old four spanned 24, so a step in *value* replaces the 1px outline that
  used to sit on every widget. `hairline` should barely register; `divider`
  is meant to be seen. (Qt has no `box-shadow`, so value is the only honest
  way to express elevation — see the study.)
* **Chrome and data colours held apart.** `state.accent` is selection and
  focus; `data.segment` is what a segment *is*. They were one namespace
  before — `AMBER` meant "hits, halos, segments" and also fed the
  spectrogram's gradient — which is why the old accent ended up doing every
  job at once and nothing on screen stood out.
* **`surface.canvas` is not `surface.ground`.** The painted views (map,
  waveform, spectrogram) draw on the darkest surface; the widgets around them
  sit on the window ground. The old `BG` was asked to be both.
* **One scale each** for spacing, radius and type, so a layout stops
  negotiating its own margins.

`palette()` and `stylesheet()` feed Qt's two styling channels. Adding a second
direction means adding a second `Tokens` instance and pointing `TOKENS` at it;
nothing else in the application refers to a colour by value.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

# --- the token structure ---------------------------------------------------


@dataclass(frozen=True)
class Surfaces:
    """Grounds, darkest to lightest."""

    canvas: QColor          # what the painted views draw on
    sunken: QColor          # wells: lists, inputs
    ground: QColor          # the window
    panel: QColor           # a grouped surface
    raised: QColor          # a control
    overlay: QColor         # menus, popups, tooltips
    hairline: QColor        # a division that should barely register
    divider: QColor         # a division meant to be seen


@dataclass(frozen=True)
class Ink:
    """Text, by prominence."""

    primary: QColor
    secondary: QColor
    muted: QColor
    bright: QColor


@dataclass(frozen=True)
class State:
    """What the interface is doing — never used for decoration."""

    accent: QColor
    accent_dim: QColor
    accent_fill: QColor     # the selected-row ground
    danger: QColor
    positive: QColor


@dataclass(frozen=True)
class Data:
    """What the audio *is*. Deliberately not the chrome palette."""

    wave: QColor
    envelope: QColor
    segment: QColor
    segment_manual: QColor
    score_low: QColor
    score_high: QColor
    unscored: QColor


@dataclass(frozen=True)
class Metrics:
    """One spacing and radius scale for the whole window."""

    xs: int
    sm: int
    md: int
    lg: int
    xl: int
    radius_sm: int
    radius_md: int
    pad_v: int              # a control's vertical padding
    pad_h: int              # a control's horizontal padding
    row_pad: int            # a list row's vertical padding


@dataclass(frozen=True)
class Typography:
    """Named roles rather than sizes chosen per widget."""

    ui: str
    mono: str
    body: float
    label: float
    micro: float
    tracking: str           # micro-label letter spacing, a QSS length


@dataclass(frozen=True)
class Tokens:
    name: str
    surface: Surfaces
    ink: Ink
    state: State
    data: Data
    metric: Metrics
    type: Typography


# --- direction A: instrument panel -----------------------------------------

INSTRUMENT = Tokens(
    name="instrument",
    surface=Surfaces(
        canvas=QColor("#0a0b0d"),
        sunken=QColor("#0d0e11"),
        ground=QColor("#101216"),
        panel=QColor("#161920"),
        raised=QColor("#1e222a"),
        overlay=QColor("#242832"),
        hairline=QColor("#20242c"),
        divider=QColor("#333944"),
    ),
    ink=Ink(
        primary=QColor("#e6e8ec"),
        secondary=QColor("#949aa6"),
        muted=QColor("#5f6673"),
        bright=QColor("#ffffff"),
    ),
    state=State(
        accent=QColor("#6fb3d9"),
        accent_dim=QColor("#2b4a5e"),
        accent_fill=QColor("#1b3543"),
        danger=QColor("#e0685f"),
        positive=QColor("#5fb87f"),
    ),
    data=Data(
        wave=QColor("#6fb3d9"),
        envelope=QColor("#b4658f"),
        segment=QColor("#d9a441"),
        segment_manual=QColor("#5fb87f"),
        score_low=QColor("#243244"),
        score_high=QColor("#f2c14e"),
        unscored=QColor("#2a2e38"),
    ),
    metric=Metrics(
        xs=2, sm=4, md=8, lg=12, xl=16,
        radius_sm=2, radius_md=3,
        pad_v=4, pad_h=10, row_pad=3,
    ),
    type=Typography(
        ui="Segoe UI",
        mono="Cascadia Mono",
        body=9.0,
        label=8.0,
        micro=7.0,
        tracking="1.2px",
    ),
)

#: The active direction. Everything else in the application reads colours,
#: spacing and type from here rather than by value.
TOKENS = INSTRUMENT


# --- helpers ---------------------------------------------------------------


def mix(a: QColor, b: QColor, t: float) -> QColor:
    """Linear blend of two colours, `t` in 0..1."""
    t = min(max(t, 0.0), 1.0)
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


def font(role: str = "body", *, bold: bool = False, mono: bool = False) -> QFont:
    """A font for a named type role — `body`, `label` or `micro`."""
    type_ = TOKENS.type
    size = {"body": type_.body, "label": type_.label, "micro": type_.micro}[role]
    result = QFont(type_.mono if mono else type_.ui)
    result.setPointSizeF(size)
    if bold:
        result.setWeight(QFont.Weight.DemiBold)
    return result


#: Faces the application loads by file. The offscreen platform ships an empty
#: font database on this machine — metrics stay correct but every glyph
#: rasterises as a tofu box, which is why every offscreen screenshot in the
#: journal before 2026-09-10 was illegible. Loading them by path costs
#: nothing on a real desktop and makes an offscreen grab of the real window
#: worth looking at (`.docs/design_studies.md`).
FONT_FILES = (
    "segoeui", "segoeuib", "segoeuisl", "segoeuil",
    "seguisym",                     # the glyph icons: anchor, arrows, transport
    "consola", "consolab", "CascadiaMono",
)


def load_fonts() -> int:
    """Register the faces by path; returns how many loaded."""
    loaded = 0
    for stem in FONT_FILES:
        if QFontDatabase.addApplicationFont(f"C:\\Windows\\Fonts\\{stem}.ttf") >= 0:
            loaded += 1
    return loaded


# --- Qt's two styling channels ---------------------------------------------


def palette(t: Tokens = TOKENS) -> QPalette:
    """What Fusion draws that a style sheet never reaches."""
    p, role = QPalette(), QPalette.ColorRole
    for which, colour in (
        (role.Window, t.surface.ground),
        (role.WindowText, t.ink.primary),
        (role.Base, t.surface.sunken),
        (role.AlternateBase, t.surface.panel),
        (role.ToolTipBase, t.surface.overlay),
        (role.ToolTipText, t.ink.primary),
        (role.Text, t.ink.primary),
        (role.PlaceholderText, t.ink.muted),
        (role.Button, t.surface.raised),
        (role.ButtonText, t.ink.secondary),
        (role.BrightText, t.ink.bright),
        (role.Highlight, t.state.accent_fill),
        (role.HighlightedText, t.ink.bright),
        (role.Link, t.state.accent),
        (role.Mid, t.surface.divider),
        (role.Dark, t.surface.canvas),
        (role.Light, t.surface.raised),
    ):
        p.setColor(which, colour)
    disabled = QPalette.ColorGroup.Disabled
    for which in (role.Text, role.ButtonText, role.WindowText):
        p.setColor(disabled, which, t.ink.muted)
    return p


def stylesheet(t: Tokens = TOKENS) -> str:
    """The application style sheet, built from the tokens.

    `text-transform`, `letter-spacing` and `font-variant: small-caps` were all
    checked against Qt before being used here (2026-09-10) — Qt honours them.
    It has no `box-shadow` and no transitions, which is why elevation is a
    step on the surface ramp and nothing animates.
    """
    s, ink, st, m, ty = t.surface, t.ink, t.state, t.metric, t.type
    micro = f"font-size: {ty.micro}pt; font-weight: 700; text-transform: uppercase; letter-spacing: {ty.tracking};"
    # `text-transform` reaches a QLabel but NOT the QGroupBox::title subcontrol
    # (checked by rendering, 2026-09-10) - size, weight and tracking do. A group
    # title is therefore styled without it, and the uppercase micro-label proper
    # is `QLabel#sectionHeader`; replacing the boxes with a real SectionHeader
    # component is the component layer's job.
    micro_title = f"font-size: {ty.micro}pt; font-weight: 700; letter-spacing: {ty.tracking};"
    return f"""
QWidget {{ color: {ink.primary.name()}; font-family: "{ty.ui}"; font-size: {ty.body}pt; }}
QMainWindow, QDialog {{ background: {s.ground.name()}; }}
QToolTip {{ background: {s.overlay.name()}; color: {ink.primary.name()};
            border: 1px solid {s.divider.name()};
            padding: {m.sm}px {m.md - 2}px; border-radius: {m.radius_sm}px; }}

QGroupBox {{ background: {s.panel.name()}; border: 1px solid {s.hairline.name()};
             border-radius: {m.radius_md}px; margin-top: {m.xl - 1}px;
             padding: {m.md}px {m.pad_h}px {m.md - 2}px {m.pad_h}px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: {m.xs}px;
                    padding: 0 3px 0 0; color: {ink.muted.name()}; {micro_title} }}

QPushButton {{ background: {s.raised.name()}; border: 1px solid {s.divider.name()};
               border-radius: {m.radius_sm}px; padding: {m.pad_v}px {m.pad_h}px;
               color: {ink.secondary.name()}; min-height: 16px; }}
QPushButton:hover {{ background: {s.overlay.name()}; color: {ink.primary.name()};
                     border-color: {st.accent_dim.name()}; }}
QPushButton:pressed {{ background: {st.accent_fill.name()}; }}
QPushButton:checked {{ background: {st.accent_fill.name()}; border-color: {st.accent.name()};
                       color: {ink.bright.name()}; }}
QPushButton:disabled {{ color: {ink.muted.name()}; background: transparent;
                        border-color: {s.hairline.name()}; }}
QPushButton:flat {{ background: transparent; border: 1px solid {s.divider.name()};
                    border-radius: {m.radius_md}px; padding: {m.xs}px {m.md}px; }}
QPushButton:flat:hover {{ background: {s.raised.name()}; }}
QPushButton#play {{ background: {st.accent_fill.name()}; border-color: {st.accent.name()};
                    color: {st.accent.name()}; font-size: 15pt; padding: 0px {m.lg}px; }}
QPushButton#play:hover {{ background: {st.accent_dim.name()}; color: {ink.bright.name()}; }}
QPushButton#anchor {{ border-color: {t.data.segment.name()}; color: {t.data.segment.name()}; }}

/* Button intent (2026-09-10, layer 2). A property selector, so `intent` is set
   once at construction — Qt re-polishes on show, not on a later change.
   `danger` stays quiet at rest and colours on hover: a destructive action must
   be identifiable without a resting toolbar shouting. */
QPushButton[intent="primary"] {{ background: {st.accent_fill.name()};
    border-color: {st.accent.name()}; color: {st.accent.name()}; }}
QPushButton[intent="primary"]:hover {{ background: {st.accent_dim.name()};
    color: {ink.bright.name()}; }}
QPushButton[intent="quiet"] {{ background: transparent; border-color: transparent;
    color: {ink.muted.name()}; }}
QPushButton[intent="quiet"]:hover {{ background: {s.raised.name()};
    border-color: {s.hairline.name()}; color: {ink.primary.name()}; }}
QPushButton[intent="danger"]:hover {{ background: {s.raised.name()};
    border-color: {st.danger.name()}; color: {st.danger.name()}; }}
QPushButton[intent="danger"]:pressed {{ background: {st.danger.name()};
    color: {s.canvas.name()}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: {s.sunken.name()}; border: 1px solid {s.hairline.name()};
    border-radius: {m.radius_sm}px; padding: {m.pad_v - 1}px {m.pad_h - 4}px;
    color: {ink.primary.name()}; selection-background-color: {st.accent_dim.name()}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {st.accent.name()}; }}
QComboBox::drop-down {{ border: none; width: {m.xl}px; }}
QComboBox QAbstractItemView {{ background: {s.overlay.name()};
                               border: 1px solid {s.divider.name()};
                               selection-background-color: {st.accent_fill.name()}; }}

QTreeView, QTableView, QListWidget {{
    background: {s.sunken.name()}; alternate-background-color: {s.sunken.name()};
    border: none; color: {ink.primary.name()};
    selection-background-color: {st.accent_fill.name()}; selection-color: {ink.bright.name()};
    gridline-color: {s.hairline.name()}; outline: none; }}
QTreeView::item, QTableView::item {{ padding: {m.row_pad}px {m.sm}px; border: none; }}
QTreeView::item:hover, QTableView::item:hover {{ background: {s.raised.name()}; }}
QTreeView::item:selected, QTableView::item:selected {{
    background: {st.accent_fill.name()}; color: {ink.bright.name()}; }}

QHeaderView {{ background: {s.ground.name()}; }}
QHeaderView::section {{ background: {s.ground.name()}; color: {ink.muted.name()};
    border: none; border-bottom: 1px solid {s.divider.name()};
    padding: {m.sm + 1}px {m.md - 2}px; {micro} }}
QHeaderView::section:hover {{ color: {ink.secondary.name()}; }}

QTabWidget::pane {{ border: none; border-top: 1px solid {s.hairline.name()};
                    top: -1px; background: {s.ground.name()}; }}
QTabBar::tab {{ background: transparent; color: {ink.muted.name()}; border: none;
    border-bottom: 2px solid transparent; padding: {m.md - 2}px {m.lg}px;
    margin-right: {m.xs}px; font-size: {ty.label}pt;
    text-transform: uppercase; letter-spacing: {ty.tracking}; }}
QTabBar::tab:selected {{ color: {ink.primary.name()}; border-bottom: 2px solid {st.accent.name()}; }}
QTabBar::tab:hover {{ color: {ink.secondary.name()}; }}

QProgressBar {{ background: {s.sunken.name()}; border: 1px solid {s.hairline.name()};
    border-radius: {m.radius_sm}px; text-align: center; color: {ink.secondary.name()};
    min-height: 14px; font-size: {ty.micro}pt; }}
QProgressBar::chunk {{ background: {st.accent_dim.name()}; border-radius: {m.radius_sm}px; }}

QSlider::groove:horizontal {{ height: 3px; background: {s.divider.name()}; border-radius: 1px; }}
QSlider::sub-page:horizontal {{ background: {st.accent.name()}; border-radius: 1px; }}
QSlider::handle:horizontal {{ background: {ink.primary.name()}; border: none;
    width: 9px; height: 9px; margin: -4px 0; border-radius: 4px; }}

QCheckBox, QRadioButton {{ spacing: {m.md - 2}px; color: {ink.secondary.name()}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 12px; height: 12px;
    border: 1px solid {s.divider.name()}; border-radius: {m.radius_sm}px;
    background: {s.sunken.name()}; }}
QCheckBox::indicator:checked {{ background: {st.accent.name()}; border-color: {st.accent.name()}; }}
QRadioButton::indicator {{ border-radius: 6px; }}
QRadioButton::indicator:checked {{ background: {st.accent.name()}; border-color: {st.accent.name()}; }}

QScrollBar:vertical {{ background: transparent; width: {m.md}px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {s.divider.name()}; border-radius: {m.sm}px;
                               min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {ink.muted.name()}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: {m.md}px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {s.divider.name()}; border-radius: {m.sm}px;
                                 min-width: 24px; }}

QSplitter::handle {{ background: {s.ground.name()}; }}
QSplitter::handle:horizontal {{ width: {m.sm + 1}px; }}
QSplitter::handle:vertical {{ height: {m.sm + 1}px; }}

QStatusBar {{ background: {s.ground.name()}; color: {ink.muted.name()};
              border-top: 1px solid {s.hairline.name()}; font-size: {ty.micro}pt; }}
QStatusBar::item {{ border: none; }}

QMenu {{ background: {s.overlay.name()}; color: {ink.primary.name()};
         border: 1px solid {s.divider.name()}; padding: {m.sm}px 0;
         border-radius: {m.radius_md}px; }}
QMenu::item {{ padding: {m.sm + 1}px {m.xl + 4}px {m.sm + 1}px {m.xl + 6}px; }}
QMenu::item:selected {{ background: {st.accent_fill.name()}; }}
QMenu::item:disabled {{ color: {ink.muted.name()}; }}
QMenu::separator {{ height: 1px; background: {s.hairline.name()}; margin: 3px {m.md - 2}px; }}

QFrame#filterPopup {{ background: {s.overlay.name()}; border: 1px solid {s.divider.name()};
                      border-radius: {m.radius_md}px; }}
QLabel#caption {{ color: {ink.muted.name()}; font-size: {ty.label}pt; }}
QLabel#nowPlaying {{ color: {ink.primary.name()}; font-weight: 600; }}
QLabel#sectionHeader {{ color: {ink.muted.name()}; {micro} }}
QWidget#header {{ background: {s.ground.name()};
                  border-bottom: 1px solid {s.hairline.name()}; }}
"""
