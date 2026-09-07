"""The application's look (2026-09-07, on the user's steer): one dark theme
in the idiom of production tools — Fusion style, a palette, a style sheet —
plus the colours the painted views (map, waveform) share with it, so the
whole window reads as one surface rather than default widgets.

`apply_theme(app)` is called once by `main()`; tests run unthemed. The
painters import the colour constants directly, so they look right either
way.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QWidget

# --- palette ---------------------------------------------------------------

BG = QColor("#1b1c21")          # window ground
PANEL = QColor("#232429")       # panels, group boxes
FIELD = QColor("#2b2c33")       # inputs, tables
RAISED = QColor("#33343c")      # buttons, headers
BORDER = QColor("#3d3e48")
TEXT = QColor("#d9dae0")
TEXT_DIM = QColor("#8f9199")
ACCENT = QColor("#4f9cf5")      # selection, play, the waveform
ACCENT_DIM = QColor("#2f5f95")
AMBER = QColor("#f0b84a")       # hits, halos, segments
GREEN = QColor("#5fc48a")       # manual segments
PINK = QColor("#ff5fa2")        # the envelope
RED = QColor("#ff5c5c")
WHITE = QColor("#f4f4f6")

# score gradient for the map: dim → bright
SCORE_LOW = QColor("#34405a")
SCORE_HIGH = QColor("#ffd166")


def mix(a: QColor, b: QColor, t: float) -> QColor:
    """Linear blend of two colours, t in 0..1."""
    t = min(max(t, 0.0), 1.0)
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


def _palette() -> QPalette:
    palette = QPalette()
    roles = QPalette.ColorRole
    palette.setColor(roles.Window, BG)
    palette.setColor(roles.WindowText, TEXT)
    palette.setColor(roles.Base, FIELD)
    palette.setColor(roles.AlternateBase, QColor("#282930"))
    palette.setColor(roles.ToolTipBase, RAISED)
    palette.setColor(roles.ToolTipText, TEXT)
    palette.setColor(roles.Text, TEXT)
    palette.setColor(roles.PlaceholderText, TEXT_DIM)
    palette.setColor(roles.Button, RAISED)
    palette.setColor(roles.ButtonText, TEXT)
    palette.setColor(roles.BrightText, WHITE)
    palette.setColor(roles.Highlight, ACCENT)
    palette.setColor(roles.HighlightedText, WHITE)
    palette.setColor(roles.Link, ACCENT)
    palette.setColor(roles.Mid, BORDER)
    palette.setColor(roles.Dark, BG)
    palette.setColor(roles.Light, RAISED)
    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, roles.Text, TEXT_DIM)
    palette.setColor(disabled, roles.ButtonText, TEXT_DIM)
    palette.setColor(disabled, roles.WindowText, TEXT_DIM)
    return palette


STYLE_SHEET = f"""
QWidget {{
    color: {TEXT.name()};
    font-size: 10pt;
}}
QMainWindow, QDialog {{ background: {BG.name()}; }}
QToolTip {{
    background: {RAISED.name()}; color: {TEXT.name()};
    border: 1px solid {BORDER.name()}; padding: 6px; border-radius: 4px;
}}
QGroupBox {{
    background: {PANEL.name()};
    border: 1px solid {BORDER.name()};
    border-radius: 6px;
    margin-top: 14px;
    padding: 8px 6px 6px 6px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 10px; padding: 0 4px;
    color: {TEXT_DIM.name()}; font-size: 8.5pt; font-weight: 600;
    letter-spacing: 1px; text-transform: uppercase;
}}
QPushButton {{
    background: {RAISED.name()}; border: 1px solid {BORDER.name()};
    border-radius: 5px; padding: 5px 12px; min-height: 18px;
}}
QPushButton:hover {{ background: #3c3d47; border-color: #4a4b56; }}
QPushButton:pressed {{ background: {ACCENT_DIM.name()}; }}
QPushButton:checked {{ background: {ACCENT_DIM.name()}; border-color: {ACCENT.name()}; color: {WHITE.name()}; }}
QPushButton:disabled {{ color: {TEXT_DIM.name()}; background: {PANEL.name()}; }}
QPushButton:flat {{ background: transparent; border: 1px solid {BORDER.name()}; border-radius: 11px; padding: 2px 9px; }}
QPushButton:flat:hover {{ background: {RAISED.name()}; }}
QPushButton#play {{ background: {ACCENT_DIM.name()}; border-color: {ACCENT.name()}; color: {WHITE.name()}; font-weight: 600; padding: 5px 18px; }}
QPushButton#play:hover {{ background: {ACCENT.name()}; }}
QPushButton#anchor {{ border-color: {AMBER.name()}; color: {AMBER.name()}; }}
QPushButton#anchor:hover {{ background: #3a3320; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: {FIELD.name()}; border: 1px solid {BORDER.name()};
    border-radius: 4px; padding: 3px 6px; selection-background-color: {ACCENT.name()};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT.name()}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {FIELD.name()}; selection-background-color: {ACCENT_DIM.name()}; }}
QTreeView, QTableView, QListWidget {{
    background: {FIELD.name()}; alternate-background-color: #303139;
    border: 1px solid {BORDER.name()}; border-radius: 4px;
    selection-background-color: {ACCENT_DIM.name()}; selection-color: {WHITE.name()};
    gridline-color: {BORDER.name()};
}}
QTreeView::item, QTableView::item {{ padding: 2px 4px; }}
QTreeView::item:selected, QTableView::item:selected {{ background: {ACCENT_DIM.name()}; }}
QHeaderView::section {{
    background: {RAISED.name()}; color: {TEXT_DIM.name()};
    border: none; border-right: 1px solid {BORDER.name()}; border-bottom: 1px solid {BORDER.name()};
    padding: 4px 6px; font-size: 8.5pt; font-weight: 600; letter-spacing: 0.5px;
}}
QTabWidget::pane {{ border: 1px solid {BORDER.name()}; border-radius: 6px; top: -1px; background: {BG.name()}; }}
QTabBar::tab {{
    background: {PANEL.name()}; color: {TEXT_DIM.name()};
    border: 1px solid {BORDER.name()}; border-bottom: none;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    padding: 6px 16px; margin-right: 2px;
}}
QTabBar::tab:selected {{ background: {BG.name()}; color: {TEXT.name()}; border-bottom: 2px solid {ACCENT.name()}; }}
QTabBar::tab:hover {{ color: {TEXT.name()}; }}
QProgressBar {{
    background: {FIELD.name()}; border: 1px solid {BORDER.name()}; border-radius: 4px;
    text-align: center; color: {TEXT.name()}; min-height: 16px;
}}
QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT_DIM.name()}, stop:1 {ACCENT.name()}); border-radius: 3px; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER.name()}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT.name()}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {WHITE.name()}; border: 1px solid {BORDER.name()};
    width: 12px; height: 12px; margin: -5px 0; border-radius: 6px;
}}
QCheckBox, QRadioButton {{ spacing: 6px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {RAISED.name()}; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #45464f; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {RAISED.name()}; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QSplitter::handle {{ background: {BG.name()}; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:vertical {{ height: 6px; }}
QStatusBar {{ background: {PANEL.name()}; color: {TEXT_DIM.name()}; border-top: 1px solid {BORDER.name()}; }}
QLabel#caption {{ color: {TEXT_DIM.name()}; }}
QLabel#nowPlaying {{ color: {TEXT.name()}; font-weight: 600; }}
"""


class SqueezableWidget(QWidget):
    """A scroll area's content that never claims a minimum width: a
    QScrollArea sizes its widget to the viewport but not below the
    widget's `minimumSizeHint`, so a wide row (five checkboxes) would
    otherwise push the panel past its pane (2026-09-07)."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())


def apply_theme(app: QApplication) -> None:
    """Fusion + the palette + the style sheet, once per application."""
    app.setStyle("Fusion")
    app.setPalette(_palette())
    font = QFont("Segoe UI", 10)
    app.setFont(font)
    app.setStyleSheet(STYLE_SHEET)
