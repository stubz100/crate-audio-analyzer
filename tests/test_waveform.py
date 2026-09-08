"""Tests for the waveform panel: the block-wise envelope and the widget's
geometry and click mapping (offscreen)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from crate.catalog import SegmentRow
from crate.waveform import DEFAULT_COLUMNS, WaveformPanel, WaveformView, _tick_step, envelope_columns

SR = 22050


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _click_file(path, at_s: float = 0.5, duration_s: float = 2.0, channels: int = 1):
    y = np.zeros(int(SR * duration_s), dtype="float32")
    start = int(at_s * SR)
    y[start : start + int(0.02 * SR)] = 0.9
    if channels == 2:
        y = np.stack([y, y * 0.5], axis=1)
    sf.write(path, y, SR)
    return path


def _mouse(kind: QEvent.Type, x: float, y: float = 60.0) -> QMouseEvent:
    pos = QPointF(x, y)
    button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    return QMouseEvent(kind, pos, pos, button, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


def _click(view: WaveformView, x: float) -> None:
    """A press and a release without movement — a click since Phase 9."""
    view.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, x))
    view.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, x))


def _drag(view: WaveformView, x0: float, x1: float) -> None:
    view.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, x0))
    view.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, (x0 + x1) / 2))
    view.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, x1))
    view.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, x1))


def _key(key) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


def test_envelope_columns_shape_duration_and_peak(tmp_path):
    env = envelope_columns(_click_file(tmp_path / "c.wav"))

    assert 0 < env.columns <= DEFAULT_COLUMNS
    assert env.duration_s == pytest.approx(2.0)
    assert env.maxs.max() == pytest.approx(0.9, abs=0.01) and env.mins.min() <= 0.0
    assert env.peak_column / env.columns == pytest.approx(0.25, abs=0.02)   # the click at 0.5 s of 2 s


def test_envelope_columns_mixes_channels_and_caps_columns(tmp_path):
    env = envelope_columns(_click_file(tmp_path / "s.wav", channels=2), columns=100)
    assert env.columns <= 100
    assert env.maxs.max() == pytest.approx(0.675, abs=0.02)                # (0.9 + 0.45) / 2


def test_envelope_of_an_empty_file(tmp_path):
    sf.write(tmp_path / "e.wav", np.zeros((0, 1), dtype="float32"), SR)
    env = envelope_columns(tmp_path / "e.wav")
    assert env.columns == 0 and env.duration_s == 0.0


def test_tick_step_grows_with_duration():
    assert _tick_step(2.0, 1000) < _tick_step(60.0, 1000) < _tick_step(900.0, 1000)


def test_view_maps_clicks_to_segments_and_times(app, tmp_path):
    view = WaveformView()
    view.resize(1000, 150)
    segments = [
        SegmentRow(1, 1, 500, 900, 1.0, "auto", 0, None),
        SegmentRow(2, 1, 600, 700, 0.5, "manual", 0, None),
    ]
    windows = [
        SegmentRow(3, 1, 0, 1000, None, "window", 0, None),
        SegmentRow(4, 1, 1000, 2000, None, "window", 0, None),
    ]
    view.load(_click_file(tmp_path / "c.wav"), "c.wav", segments, attack_ms=10.0, decay_ms=100.0, windows=windows)

    assert view.loaded and view.duration_s == pytest.approx(2.0)
    assert view.segment_at(1.5) is None                                    # windows are not clickable
    assert view.segment_at(0.65).id == 2                                   # the nested, shorter one wins
    assert view.segment_at(0.55).id == 1 and view.segment_at(1.5) is None
    rect = view._plot_rect()
    assert view.time_at_x(rect.left()) == 0.0
    assert view.time_at_x(rect.right()) == pytest.approx(2.0)
    header = view._header_text()
    assert "attack 10 ms" in header and "decay 100 ms" in header and "2 segments (1 manual)" in header
    assert "2 CLAP windows" in header

    clicked: list[int] = []
    seeks: list[int] = []
    view.segment_clicked.connect(clicked.append)
    view.position_clicked.connect(seeks.append)
    _click(view, view._x_of(0.65, rect))
    assert clicked == [2]
    _click(view, view._x_of(1.5, rect))
    assert len(seeks) == 1 and seeks[0] == pytest.approx(1500, abs=1)

    view.set_selected_segment(1)
    view.set_position_ms(700)
    assert not view.grab().isNull()                                        # paints with everything on
    view.set_selected_segment(4)                                           # a window as the hit
    assert not view.grab().isNull()

    view.load(tmp_path / "missing.wav", "missing", [], None, None)
    assert not view.loaded and view._error
    view.clear()
    assert not view.loaded and view._error is None


def test_a_long_file_is_read_on_a_thread(app, tmp_path):
    from crate import waveform as wf

    path = tmp_path / "long.wav"
    sf.write(path, np.zeros(int(SR * 2.0), dtype="float32"), SR)
    view = WaveformView()
    original = wf.INLINE_MAX_SECONDS
    wf.INLINE_MAX_SECONDS = 1.0                       # make the 2 s file count as long
    try:
        view.load(path, "long.wav", [], None, None)
        assert not view.loaded and "reading" in (view._error or "")
        view.wait_for_load()
        deadline = 200
        while not view.loaded and deadline:
            app.processEvents()
            deadline -= 1
        assert view.loaded and view.duration_s == pytest.approx(2.0)
    finally:
        wf.INLINE_MAX_SECONDS = original


# --- Phase 9 (2026-09-08): markers drag, segments draw, everything staged until Save ---


def test_markers_drag_and_segments_draw_staged_until_saved(app, tmp_path):
    view = WaveformView()
    view.resize(1000, 150)
    view.load(_click_file(tmp_path / "c.wav"), "c.wav", [SegmentRow(1, 1, 500, 1000, 1.0, "auto", 0, None)], None, None)
    rect = view._plot_rect()
    x = lambda seconds: view._x_of(seconds, rect)  # noqa: E731
    changes: list[int] = []
    view.staged_changed.connect(lambda: changes.append(1))

    assert view.marker_at(x(1.0)) == (1, "end") and view.marker_at(x(0.5) + 3) == (1, "start")
    assert view.marker_at(x(1.5)) is None and not view.has_staged

    _drag(view, x(1.0), x(1.4))                                    # the end marker to 1.4 s: staged
    assert view.staged() == [(1, 500, 1400)] and view.has_staged and changes
    assert view.bounds_of(1) == (500, 1400) and "1 unsaved" in view._header_text()
    assert not view.grab().isNull()                                # paints the dashed, unsaved span

    _drag(view, x(0.5), x(1.9))                                    # the start cannot cross the end
    assert view.bounds_of(1) == (1399, 1400)
    view.stage_edit(1, 500, 1000)                                  # back where it was: nothing to save
    assert not view.has_staged and view.staged() == []

    seeks: list[int] = []
    view.position_clicked.connect(seeks.append)
    _click(view, x(1.5))                                           # a click is still a click
    assert len(seeks) == 1 and seeks[0] == pytest.approx(1500, abs=1)

    view.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, x(1.8)))
    view.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, x(1.8) + 1))  # under the threshold: nothing yet
    assert not view.has_staged
    view.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, x(1.5)))     # drawn leftwards from 1.8 s
    view.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, x(1.5)))
    (segment_id, start_ms, end_ms), = view.staged()
    assert segment_id is None and start_ms == pytest.approx(1500, abs=1) and end_ms == pytest.approx(1800, abs=1)
    assert view.marker_at(x(1.5)) == (-1, "start") and len(seeks) == 1   # a draft's markers grab; no seek
    assert "1 unsaved" in view._header_text()

    view.keyPressEvent(_key(Qt.Key.Key_Escape))                    # Esc discards
    assert not view.has_staged

    deletes: list[int] = []
    view.delete_requested.connect(deletes.append)
    view.keyPressEvent(_key(Qt.Key.Key_Delete))                    # nothing selected: nothing asked
    view.set_selected_segment(1)
    assert view.can_delete
    view.keyPressEvent(_key(Qt.Key.Key_Delete))
    assert deletes == [1]

    view.add_draft(100, 200)
    view.load(_click_file(tmp_path / "d.wav"), "d.wav", [], None, None)   # another sample: staging dropped
    assert not view.has_staged and view.staged() == []
    assert view.add_draft(-50, 5000) == -1 and view.bounds_of(-1) == (0, 2000)   # clipped to the file


def test_panel_buttons_follow_staging_and_selection(app, tmp_path):
    panel = WaveformPanel()
    view = panel.view
    view.load(_click_file(tmp_path / "c.wav"), "c.wav", [SegmentRow(1, 1, 500, 1000, 1.0, "auto", 0, None)],
              None, None, windows=[SegmentRow(9, 1, 0, 2000, None, "window", 0, None)])
    saves: list[int] = []
    deletes: list[int] = []
    panel.save_requested.connect(lambda: saves.append(1))
    panel.delete_requested.connect(deletes.append)

    assert not panel._save.isEnabled() and not panel._discard.isEnabled() and not panel._delete.isEnabled()
    view.stage_edit(1, 600, 1000)
    assert panel._save.isEnabled() and panel._save.text() == "Save segment" and panel._discard.isEnabled()
    view.add_draft(1200, 1300)
    assert panel._save.text() == "Save 2 segments"
    panel._save.click()
    assert saves == [1]
    panel._discard.click()
    assert not view.has_staged and not panel._save.isEnabled()

    view.set_selected_segment(9)                                   # a CLAP window: not deletable
    assert not panel._delete.isEnabled()
    view.set_selected_segment(1)
    assert panel._delete.isEnabled()
    panel._delete.click()
    assert deletes == [1]
