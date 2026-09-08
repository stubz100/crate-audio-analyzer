"""The waveform panel — spec §9.2's preview strip, placed in the bottom
panel on the user's steer (2026-09-07) instead of the header.

Shows the selected sample's waveform, every segment as a begin/end marker
pair with its strength, the measured attack and decay (the envelope the
Amplitude axis reads, §5.1 — the closest thing to an ADSR a recording has),
and the preview's playhead. Clicking inside a segment selects it (which
previews it); clicking elsewhere seeks. Display only for now: dragging
markers and Save / Delete segment are Phase 9.

The envelope is read in blocks (`soundfile`), so a 16-minute ambience costs
one pass over the file and never more memory than one block.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from .catalog import SegmentRow
from .theme import ACCENT, AMBER, BG, BORDER, GREEN, PINK, TEXT, TEXT_DIM, WHITE

log = logging.getLogger(__name__)

DEFAULT_COLUMNS = 1200
_TOP = 18.0        # title strip
_BOTTOM = 16.0     # time axis
_SIDE = 6.0
_TICK_STEPS = (0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)
_DECAY_LEVEL = 0.1  # −20 dB, the level analysis.decay_ms is measured to
INLINE_MAX_SECONDS = 30.0  # longer files read on a thread: a 16-minute ambience took 4.7 s


@dataclass
class Envelope:
    mins: np.ndarray
    maxs: np.ndarray
    duration_s: float
    peak_column: int

    @property
    def columns(self) -> int:
        return int(self.mins.size)


def envelope_columns(path: Path | str, columns: int = DEFAULT_COLUMNS) -> Envelope:
    """Per-column min/max of the mono mix, at most `columns` columns."""
    with sf.SoundFile(str(path)) as handle:
        frames, sr = handle.frames, handle.samplerate
        if frames <= 0:
            return Envelope(np.zeros(0), np.zeros(0), 0.0, 0)
        per = max(1, -(-frames // columns))
        n = -(-frames // per)
        mins = np.full(n, np.nan)
        maxs = np.full(n, np.nan)
        col = 0
        for block in handle.blocks(blocksize=per * 64, dtype="float32", always_2d=True):
            mono = block.mean(axis=1)
            k = -(-len(mono) // per)
            padded = np.full(k * per, np.nan, dtype=np.float32)
            padded[: len(mono)] = mono
            chunk = padded.reshape(k, per)
            mins[col : col + k] = np.nanmin(chunk, axis=1)
            maxs[col : col + k] = np.nanmax(chunk, axis=1)
            col += k
    mins = np.nan_to_num(mins[:col])
    maxs = np.nan_to_num(maxs[:col])
    amplitude = np.maximum(np.abs(mins), np.abs(maxs))
    peak = int(np.argmax(amplitude)) if amplitude.size else 0
    return Envelope(mins, maxs, frames / sr, peak)


def _alpha(colour: QColor, alpha: int) -> QColor:
    out = QColor(colour)
    out.setAlpha(alpha)
    return out


def _tick_step(duration_s: float, width_px: float, min_px: float = 70.0) -> float:
    for step in _TICK_STEPS:
        if duration_s <= 0 or width_px * step / duration_s >= min_px:
            return step
    return _TICK_STEPS[-1]


class _EnvelopeThread(QThread):
    done = Signal(int, object)          # generation, Envelope
    failed = Signal(int, str)

    def __init__(self, generation: int, path: Path | str, parent=None) -> None:
        super().__init__(parent)
        self._generation = generation
        self._path = path

    def run(self) -> None:  # worker thread
        try:
            self.done.emit(self._generation, envelope_columns(self._path))
        except Exception as exc:  # noqa: BLE001 - shown in the panel
            self.failed.emit(self._generation, f"{type(exc).__name__}: {exc}")


class WaveformView(QWidget):
    segment_clicked = Signal(int)      # a click inside a segment's span
    position_clicked = Signal(int)     # a click elsewhere: milliseconds

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(110)
        self._env: Envelope | None = None
        self._title = ""
        self._error: str | None = None
        self._segments: list[SegmentRow] = []
        self._windows: list[SegmentRow] = []      # the CLAP windows of a long file (§6.4)
        self._selected_segment: int | None = None
        self._attack_ms: float | None = None
        self._decay_ms: float | None = None
        self._position_ms: int | None = None
        self._generation = 0
        self._thread: _EnvelopeThread | None = None

    # --- data in ---

    def load(
        self,
        path: Path | str,
        title: str,
        segments: list[SegmentRow],
        attack_ms: float | None,
        decay_ms: float | None,
        windows: list[SegmentRow] = (),
    ) -> None:
        self._title = title
        self._segments = list(segments)
        self._windows = list(windows)
        self._attack_ms = attack_ms
        self._decay_ms = decay_ms
        self._selected_segment = None
        self._position_ms = None
        self._generation += 1
        try:
            seconds = sf.info(str(path)).duration
        except Exception as exc:  # noqa: BLE001 - an unreadable file is shown, not raised
            self._env = None
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("waveform unavailable for %s: %s", path, exc)
            self.update()
            return
        if seconds <= INLINE_MAX_SECONDS:
            try:
                self._env = envelope_columns(path)
                self._error = None
            except Exception as exc:  # noqa: BLE001
                self._env = None
                self._error = f"{type(exc).__name__}: {exc}"
                log.warning("waveform unavailable for %s: %s", path, exc)
            self.update()
            return
        # A long file: read it on a thread and show it when it lands; a newer
        # load in the meantime wins (generation counter).
        self._env = None
        self._error = f"reading {seconds / 60:.1f} min of audio…"
        self.update()
        thread = _EnvelopeThread(self._generation, path, self)
        thread.done.connect(self._on_envelope_ready)
        thread.failed.connect(self._on_envelope_failed)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        thread.start()

    def _on_envelope_ready(self, generation: int, env: Envelope) -> None:
        if generation != self._generation:
            return
        self._env = env
        self._error = None
        self._thread = None
        self.update()

    def _on_envelope_failed(self, generation: int, error: str) -> None:
        if generation != self._generation:
            return
        self._error = error
        self._thread = None
        self.update()

    @property
    def loading(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def wait_for_load(self, timeout_ms: int = 60_000) -> None:
        """Block until a threaded read lands (tests; window close)."""
        if self._thread is not None:
            self._thread.wait(timeout_ms)

    def clear(self) -> None:
        self._generation += 1
        self._env = None
        self._title = ""
        self._error = None
        self._segments = []
        self._windows = []
        self._selected_segment = None
        self._position_ms = None
        self.update()

    def set_segments(self, segments: list[SegmentRow]) -> None:
        self._segments = list(segments)
        self.update()

    def set_selected_segment(self, segment_id: int | None) -> None:
        if segment_id != self._selected_segment:
            self._selected_segment = segment_id
            self.update()

    def set_position_ms(self, position_ms: int | None) -> None:
        if position_ms != self._position_ms:
            self._position_ms = position_ms
            self.update()

    @property
    def loaded(self) -> bool:
        return self._env is not None

    @property
    def duration_s(self) -> float:
        return self._env.duration_s if self._env is not None else 0.0

    # --- geometry ---

    def _plot_rect(self) -> QRectF:
        return QRectF(_SIDE, _TOP, max(self.width() - 2 * _SIDE, 1.0), max(self.height() - _TOP - _BOTTOM, 1.0))

    def _x_of(self, seconds: float, rect: QRectF) -> float:
        if self._env is None or self._env.duration_s <= 0:
            return rect.left()
        return rect.left() + rect.width() * seconds / self._env.duration_s

    def time_at_x(self, x: float) -> float:
        """Seconds at a widget x — the inverse of `_x_of`, for clicks."""
        rect = self._plot_rect()
        if self._env is None or rect.width() <= 0:
            return 0.0
        return float(np.clip((x - rect.left()) / rect.width(), 0.0, 1.0) * self._env.duration_s)

    def segment_at(self, seconds: float) -> SegmentRow | None:
        """The shortest segment spanning `seconds` (nested manual ones win)."""
        ms = seconds * 1000
        inside = [s for s in self._segments if s.start_ms <= ms <= s.end_ms]
        return min(inside, key=lambda s: s.length_ms) if inside else None

    # --- painting ---

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BG)
        rect = self._plot_rect()
        painter.setPen(TEXT)
        painter.drawText(QRectF(_SIDE, 2, self.width() - 2 * _SIDE, _TOP - 2), Qt.AlignmentFlag.AlignLeft, self._header_text())
        if self._env is None:
            painter.setPen(TEXT_DIM)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._error or "select a sample to see its waveform")
            painter.end()
            return
        env = self._env
        mid = rect.center().y()
        half = rect.height() / 2 - 2
        painter.setPen(QPen(BORDER, 1))
        painter.drawLine(QPointF(rect.left(), mid), QPointF(rect.right(), mid))

        self._paint_windows(painter, rect)
        self._paint_segments(painter, rect)

        if env.columns:
            path = QPainterPath()
            xs = rect.left() + (np.arange(env.columns) + 0.5) / env.columns * rect.width()
            path.moveTo(xs[0], mid - float(env.maxs[0]) * half)
            for x, v in zip(xs[1:], env.maxs[1:]):
                path.lineTo(float(x), mid - float(v) * half)
            for x, v in zip(xs[::-1], env.mins[::-1]):
                path.lineTo(float(x), mid - float(v) * half)
            path.closeSubpath()
            painter.setPen(QPen(ACCENT, 1))
            painter.setBrush(_alpha(ACCENT, 150))
            painter.drawPath(path)

        self._paint_envelope(painter, rect, mid, half)
        self._paint_axis(painter, rect)
        if self._position_ms is not None:
            x = self._x_of(self._position_ms / 1000, rect)
            painter.setPen(QPen(WHITE, 1.5))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        painter.end()

    def _header_text(self) -> str:
        if self._env is None:
            return self._title
        parts = [f"{self._title}  ·  {self._env.duration_s:.3f} s"]
        if self._attack_ms is not None:
            parts.append(f"attack {self._attack_ms:.0f} ms")
        if self._decay_ms is not None:
            parts.append(f"decay {self._decay_ms:.0f} ms (to −20 dB)")
        if self._segments:
            manual = sum(1 for s in self._segments if s.detection_method == "manual")
            parts.append(f"{len(self._segments)} segments" + (f" ({manual} manual)" if manual else ""))
        if self._windows:
            parts.append(f"{len(self._windows)} CLAP windows")
        return "  ·  ".join(parts)

    def _paint_windows(self, painter: QPainter, rect: QRectF) -> None:
        """The CLAP windows as a thin strip along the bottom of the plot —
        where the model looked, not slices anyone chose — and the selected
        one (a hit) as a band across the full height."""
        strip = 5.0
        for win in self._windows:
            x0 = self._x_of(win.start_ms / 1000, rect)
            x1 = self._x_of(win.end_ms / 1000, rect)
            selected = win.id == self._selected_segment
            painter.setPen(Qt.PenStyle.NoPen)
            if selected:
                painter.setBrush(_alpha(ACCENT, 45))
                painter.drawRect(QRectF(x0, rect.top(), max(x1 - x0, 1.0), rect.height()))
                painter.setPen(QPen(ACCENT, 2.0))
                painter.drawLine(QPointF(x0, rect.top()), QPointF(x0, rect.bottom()))
                painter.drawLine(QPointF(x1, rect.top()), QPointF(x1, rect.bottom()))
                if x1 - x0 > 50:
                    painter.setPen(ACCENT)
                    painter.drawText(
                        QRectF(x0 + 2, rect.top() + 1, x1 - x0 - 4, 14), Qt.AlignmentFlag.AlignRight, "window"
                    )
                painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ACCENT if selected else _alpha(ACCENT, 70))
            painter.drawRect(QRectF(x0 + 0.5, rect.bottom() - strip, max(x1 - x0 - 1.0, 1.0), strip))

    def _paint_segments(self, painter: QPainter, rect: QRectF) -> None:
        for seg in self._segments:
            x0 = self._x_of(seg.start_ms / 1000, rect)
            x1 = self._x_of(seg.end_ms / 1000, rect)
            selected = seg.id == self._selected_segment
            manual = seg.detection_method == "manual"
            fill = _alpha(GREEN, 80 if selected else 40) if manual else _alpha(AMBER, 90 if selected else 45)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRect(QRectF(x0, rect.top(), max(x1 - x0, 1.0), rect.height()))
            edge = GREEN if manual else AMBER
            painter.setPen(QPen(edge, 2.0 if selected else 1.0))
            painter.drawLine(QPointF(x0, rect.top()), QPointF(x0, rect.bottom()))
            painter.drawLine(QPointF(x1, rect.top()), QPointF(x1, rect.bottom()))
            label = f"{seg.strength:.2f}" if seg.strength is not None else ("manual" if manual else "")
            if seg.needs_review:
                label = "review!"
            if label and x1 - x0 > 28:
                painter.setPen(edge)
                painter.drawText(QRectF(x0 + 2, rect.top() + 1, x1 - x0 - 4, 14), Qt.AlignmentFlag.AlignLeft, label)

    def _paint_envelope(self, painter: QPainter, rect: QRectF, mid: float, half: float) -> None:
        if self._env is None or self._attack_ms is None or self._env.columns == 0:
            return
        peak_t = (self._env.peak_column + 0.5) / self._env.columns * self._env.duration_s
        start_t = max(0.0, peak_t - self._attack_ms / 1000)
        points = [QPointF(self._x_of(start_t, rect), mid), QPointF(self._x_of(peak_t, rect), mid - half)]
        if self._decay_ms is not None:
            end_t = min(self._env.duration_s, peak_t + self._decay_ms / 1000)
            points.append(QPointF(self._x_of(end_t, rect), mid - half * _DECAY_LEVEL))
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        painter.setPen(QPen(PINK, 1.5, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    def _paint_axis(self, painter: QPainter, rect: QRectF) -> None:
        if self._env is None or self._env.duration_s <= 0:
            return
        step = _tick_step(self._env.duration_s, rect.width())
        painter.setPen(TEXT_DIM)
        t = 0.0
        while t <= self._env.duration_s + 1e-9:
            x = self._x_of(t, rect)
            painter.drawLine(QPointF(x, rect.bottom()), QPointF(x, rect.bottom() + 4))
            label = f"{t:.2f}" if step < 1 else (f"{t:.0f}" if step < 60 else f"{int(t // 60)}:{int(t % 60):02d}")
            painter.drawText(QRectF(x - 30, rect.bottom() + 3, 60, 13), Qt.AlignmentFlag.AlignHCenter, label)
            t += step

    # --- interaction ---

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._env is None:
            return
        seconds = self.time_at_x(event.position().x())
        segment = self.segment_at(seconds)
        if segment is not None:
            self.segment_clicked.emit(segment.id)
        else:
            self.position_clicked.emit(int(seconds * 1000))
