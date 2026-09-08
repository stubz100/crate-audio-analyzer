"""Tests for the waveform panel: the block-wise envelope and the widget's
geometry and click mapping (offscreen)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from crate.catalog import SegmentRow
from crate.waveform import DEFAULT_COLUMNS, WaveformView, _tick_step, envelope_columns

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


def _press(x: float, y: float = 60.0) -> QMouseEvent:
    pos = QPointF(x, y)
    return QMouseEvent(
        QEvent.Type.MouseButtonPress, pos, pos, Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )


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
    view.mousePressEvent(_press(view._x_of(0.65, rect)))
    assert clicked == [2]
    view.mousePressEvent(_press(view._x_of(1.5, rect)))
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
